"""
TaoHash Marketplace Allocator Planner.

This module implements the core marketplace algorithm. It consumes a
PlanningContext (prepared by the rules pipeline) and produces:

  * AllocationPlan: a list of miner moves
  * TickDiagnostics: a compact explanation of targets, budgets, and moves

Key Algorithm Properties:
  * Price-time ranking: highest-paying orders fill first, ties broken by queue time
  * Greedy fill: each order receives hashrate up to its cap before moving to the next
  * Exponential damping: market rebalances happen gradually to avoid thrashing
  * Hard-drain: miners on ineligible/unknown orders are evacuated immediately

Entry Point:
  plan_tick(ctx: PlanningContext) -> (AllocationPlan, TickDiagnostics)

Called By:
  order_allocator.py -> _run_once() invokes plan_tick() after rules are applied
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from src.marketplace_allocator.types import MinerSnapshot
from src.marketplace_allocator.rules import PlanningContext
from src.marketplace_allocator.types import (
    AllocationPlan,
    MoveReason,
    OrderDiagnostics,
    PlanMove,
    TickDiagnostics,
)


@dataclass
class _AvailableMiners:
    """Categorized miners available to donate hashrate to receivers.

    Miners are drawn in priority order: idle -> forced -> soft.

    Attributes:
        idle: Miners on default labels (not assigned to any order). First choice
              since moving them doesn't disrupt existing allocations.
        forced: Miners on hard-drain orders (ineligible/unhealthy). Must be moved
                regardless of cooldown - these orders cannot receive hashrate.
        soft_by_order: Miners on oversupplied eligible orders, keyed by order_id.
                       Lowest-priority orders moved first to minimize disruption
                       to high-paying orders.
    """

    idle: list["MinerSnapshot"]
    forced: list["MinerSnapshot"]
    soft_by_order: dict[str, list["MinerSnapshot"]]


def _compute_alpha(rate: float, dt_seconds: float, interval_seconds: float) -> float:
    """
    Compute exponential smoothing coefficient.

    alpha = 1 - (1 - rate)^(dt/interval)
    """

    if interval_seconds <= 0:
        return 1.0
    if rate <= 0:
        return 0.0
    if rate >= 1:
        return 1.0
    # Guard against floating issues for extreme dt.
    exponent = max(0.0, float(dt_seconds) / float(interval_seconds))
    return 1.0 - math.pow(1.0 - float(rate), exponent)


def _rank_orders(ctx: PlanningContext):
    """Sort eligible orders by priority: highest price first, then earliest queue time."""
    return sorted(
        ctx.eligible_orders,
        key=lambda order: (-order.price_btc_eh_day, order.queue_position_time),
    )


def _is_default_label(ctx: PlanningContext, label: str) -> bool:
    """Check if a pool label is a default/idle label (not assigned to any order)."""
    return label in ctx.config.default_labels


def _order_id_from_label(ctx: PlanningContext, label: str) -> Optional[str]:
    """Extract order_id from a pool label. Returns None if it's a default label."""
    if _is_default_label(ctx, label):
        return None
    return label


def plan_tick(ctx: PlanningContext) -> tuple[AllocationPlan, TickDiagnostics]:
    """Plan one allocator tick.

    Overview:
        The planner answers: "Given current miner positions and order priorities,
        which miners should move where?" It computes ideal targets for each order,
        compares to current allocation, and generates moves to close the gap.
        Moves are damped (gradual) to avoid thrashing, except for hard-drains
        which happen immediately.

    Algorithm Steps:
        1. Rank orders by price (desc), then queue time (asc)
        2. Compute total supply from all miners
        3. Compute targets via greedy fill (high-priority orders fill first)
        4. Compute current allocation from each miner's pool_label
        5. Set target=0 for hard-drain orders (must evacuate)
        6. Compute movement budgets: damped for market moves, full for hard-drain
        7. Build donor pools: idle → forced → soft (in priority order)
        8. Identify receivers (orders where target > current)
        9. Execute moves:
           a) Fill receivers by drawing from donor pools
           b) Evacuate any remaining forced-drain miners to default
           c) Shed excess from oversupplied orders to default
        10. Build diagnostics for telemetry

    Args:
        ctx: PlanningContext with eligibility/health rules already applied.

    Returns:
        (AllocationPlan, TickDiagnostics) - moves to execute and telemetry.

    Note:
        This function is pure and deterministic. All side effects
        (DB/Redis writes) happen in the executor.
    """

    now = ctx.snapshot.now
    ranked = _rank_orders(ctx)
    eligible_orders = {order.order_id for order in ranked}

    # ----------------------------------------------------------------------
    # Step 1-2: Total Supply
    # Sum all miner hashrates to know how much capacity we have to allocate.
    # ----------------------------------------------------------------------
    supply_eh_s = float(sum(miner.hashrate_eh_s for miner in ctx.snapshot.miners))

    # ----------------------------------------------------------------------
    # Step 3: Compute Ideal Targets (Greedy Fill)
    # Walk orders in priority order, giving each as much hashrate as it can
    # take (up to its cap) before moving to the next. This ensures highest-
    # paying orders are fully satisfied first.
    # ----------------------------------------------------------------------
    targets: dict[str, float] = {}
    # Effective caps include overshoot allowance to prevent rejecting miners
    # whose hashrate slightly exceeds the remaining capacity.
    effective_caps: dict[str, float] = {}
    remaining = supply_eh_s
    for order in ranked:
        cap = float("inf")
        if order.limit_eh_s is not None:
            try:
                cap = float(order.limit_eh_s)
            except Exception:
                cap = float("inf")
            cap = cap * (1.0 + float(ctx.config.overshoot_pct))

        effective_caps[order.order_id] = cap

        if remaining <= 0:
            targets[order.order_id] = 0.0
            continue

        target = min(cap, remaining)
        if target < 0:
            target = 0.0

        targets[order.order_id] = target
        remaining -= target

    # ----------------------------------------------------------------------
    # Step 4: Compute Current Allocation
    # Group miners by their current pool_label to see where hashrate is now.
    # This lets us compute deltas (target - current) for each order.
    # ----------------------------------------------------------------------
    current_allocation: dict[str, float] = {}
    miners_by_order: dict[str, list["MinerSnapshot"]] = {}
    idle_miners: list["MinerSnapshot"] = []
    currently_mined: set[str] = set()

    for miner in ctx.snapshot.miners:
        order_id = _order_id_from_label(ctx, miner.pool_label)
        if order_id is None:
            idle_miners.append(miner)
            continue

        currently_mined.add(order_id)
        current_allocation[order_id] = current_allocation.get(order_id, 0.0) + float(
            miner.hashrate_eh_s
        )
        miners_by_order.setdefault(order_id, []).append(miner)

    # ----------------------------------------------------------------------
    # Step 5: Hard Drain Orders
    # Orders flagged by rules (ineligible, unhealthy pools) must be emptied.
    # Set their target to 0 so all their miners become donors.
    # ----------------------------------------------------------------------
    hard_drain_ids = set(ctx.hard_drain_ids)
    for order_id in hard_drain_ids:
        targets[order_id] = 0.0

    # ----------------------------------------------------------------------
    # Step 6: Movement Budgets
    # Limit how much hashrate can move per tick to avoid thrashing. Market
    # moves use exponential damping (alpha); hard-drain moves get full budget.
    # ----------------------------------------------------------------------
    alpha = _compute_alpha(
        ctx.config.rebalance_rate,
        ctx.tick_elapsed_clamped_seconds,
        float(ctx.config.interval_seconds),
    )
    if ctx.freeze_market_moves:
        alpha = 0.0

    budget: dict[str, float] = {}

    deadband = float(ctx.config.rebalance_threshold_eh_s)

    # Eligible orders: damped budget
    for order in ranked:
        order_id = order.order_id
        delta = targets.get(order_id, 0.0) - current_allocation.get(order_id, 0.0)
        if abs(delta) <= deadband:
            budget[order_id] = 0.0
        else:
            budget[order_id] = alpha * abs(delta)

    # Hard drain orders: full drain budget
    for order_id in hard_drain_ids:
        budget[order_id] = current_allocation.get(order_id, 0.0)

    # ----------------------------------------------------------------------
    # Step 7: Build Donor Pools
    # Categorize miners that can be moved: idle (unassigned), forced (on bad
    # orders), and soft (on oversupplied orders). See _AvailableMiners docstring.
    # ----------------------------------------------------------------------
    donors_idle = sorted(idle_miners, key=lambda miner: -miner.hashrate_eh_s)

    donors_forced: list = []
    for order_id in hard_drain_ids:
        donors_forced.extend(miners_by_order.get(order_id, []))
    donors_forced.sort(key=lambda miner: -miner.hashrate_eh_s)

    # Soft donors: eligible orders that are oversupplied (current > target)
    rank_index = {order.order_id: rank for rank, order in enumerate(ranked)}
    soft_donor_order_ids = [
        order_id
        for order_id in eligible_orders
        if current_allocation.get(order_id, 0.0) > targets.get(order_id, 0.0) + deadband
    ]
    soft_donor_order_ids.sort(
        key=lambda order_id: rank_index.get(order_id, 10**9), reverse=True
    )

    donors_soft_by_order: dict[str, list] = {}
    for order_id in soft_donor_order_ids:
        order_miners = list(miners_by_order.get(order_id, []))
        order_miners.sort(key=lambda miner: -miner.hashrate_eh_s)
        donors_soft_by_order[order_id] = order_miners

    donors = _AvailableMiners(
        idle=donors_idle, forced=donors_forced, soft_by_order=donors_soft_by_order
    )

    # ----------------------------------------------------------------------
    # Step 8: Identify Receivers
    # Orders that need more hashrate (target > current). Processed in priority
    # order so highest-paying orders receive miners first.
    # ----------------------------------------------------------------------
    receivers: list[str] = []
    for order in ranked:
        order_id = order.order_id
        need_total = targets.get(order_id, 0.0) - current_allocation.get(order_id, 0.0)
        if need_total > deadband:
            receivers.append(order_id)

    # ----------------------------------------------------------------------
    # Miner Selection Helpers
    # These functions draw miners from donor pools while respecting cooldowns
    # (recently moved miners) and capacity limits (miner too large for order).
    # ----------------------------------------------------------------------
    moved_ips: set[str] = set()  # Track miners moved this tick to avoid double-moves

    def in_cooldown(worker_ip: str) -> bool:
        """Check if miner was recently moved and should not be moved again yet."""
        return ctx.runtime.miner_in_cooldown(
            worker_ip, now, ctx.config.miner_cooldown_seconds
        )

    _FIT_EPS = 1e-12  # Tolerance for floating-point hashrate comparisons

    def _fits(miner: MinerSnapshot, max_hashrate: float) -> bool:
        """Check if miner's hashrate fits within the remaining capacity."""
        if math.isinf(max_hashrate):
            return True
        return float(miner.hashrate_eh_s) <= float(max_hashrate) + _FIT_EPS

    def pick_idle(max_hashrate: float) -> Optional[tuple[MinerSnapshot, None]]:
        """Pick an idle miner that fits within max_hashrate.

        Returns (miner, None) since idle miners have no source order.
        Skips miners in cooldown. Keeps large miners in pool for larger-cap orders.
        """
        for idx, miner in enumerate(donors.idle):
            if miner.worker_ip in moved_ips:
                continue
            if in_cooldown(miner.worker_ip):
                continue
            if not _fits(miner, max_hashrate):
                continue
            donors.idle.pop(idx)
            return miner, None
        return None

    def pick_forced(
        max_hashrate: float, allow_cooldown: bool
    ) -> Optional[tuple[MinerSnapshot, Optional[str]]]:
        """Pick a miner from forced-drain orders (must evacuate).

        Returns (miner, source_order_id). Forced miners can bypass cooldown
        when allow_cooldown=True because they MUST be moved off bad orders.
        """
        for idx, miner in enumerate(donors.forced):
            if miner.worker_ip in moved_ips:
                continue
            if (not allow_cooldown) and in_cooldown(miner.worker_ip):
                continue
            if not _fits(miner, max_hashrate):
                continue
            donors.forced.pop(idx)
            source_order_id = _order_id_from_label(ctx, miner.pool_label)
            return miner, source_order_id
        return None

    def pick_soft(
        max_hashrate: float, allow_cooldown: bool
    ) -> Optional[tuple[MinerSnapshot, str]]:
        """Pick a miner from an oversupplied eligible order.

        Returns (miner, source_order_id). Iterates lowest-priority orders first
        to minimize disruption to high-paying customers.
        """
        for donor_order_id in soft_donor_order_ids:
            pool = donors.soft_by_order.get(donor_order_id)
            if not pool:
                continue
            for idx, miner in enumerate(pool):
                if miner.worker_ip in moved_ips:
                    continue
                if (not allow_cooldown) and in_cooldown(miner.worker_ip):
                    continue
                if not _fits(miner, max_hashrate):
                    continue
                pool.pop(idx)
                return miner, donor_order_id
        return None

    # ----------------------------------------------------------------------
    # Step 9: Execute Moves (Plan Construction)
    # Fill receivers from donors, then evacuate remaining forced miners,
    # then shed excess from oversupplied orders. Respects max_switches limit.
    # ----------------------------------------------------------------------
    moves: list[PlanMove] = []

    def record_move(
        miner: MinerSnapshot,
        source_order_id: Optional[str],
        dest_order_id: Optional[str],
        reason: MoveReason,
    ) -> None:
        """Record a planned move and update local allocation accounting.

        Also marks the miner as moved so it won't be picked again this tick.
        """
        nonlocal moves
        moved_ips.add(miner.worker_ip)
        moves.append(
            PlanMove(
                worker_ip=miner.worker_ip,
                from_label=miner.pool_label,
                to_order_id=dest_order_id,
                reason=reason,
                hashrate_eh_s=float(miner.hashrate_eh_s),
            )
        )
        # Update local accounting
        if source_order_id:
            current_allocation[source_order_id] = max(
                0.0,
                current_allocation.get(source_order_id, 0.0)
                - float(miner.hashrate_eh_s),
            )
        if dest_order_id:
            current_allocation[dest_order_id] = current_allocation.get(
                dest_order_id, 0.0
            ) + float(miner.hashrate_eh_s)

    max_switches = int(ctx.config.max_switches_per_tick)

    # Phase 1: Fill receivers by drawing from donor pools (idle -> forced -> soft)
    for receiver_id in receivers:
        if len(moves) >= max_switches:
            break

        need_total = targets.get(receiver_id, 0.0) - current_allocation.get(
            receiver_id, 0.0
        )
        if need_total <= deadband:
            continue

        need = min(need_total, budget.get(receiver_id, 0.0))
        if need <= 0:
            continue

        while need > deadband and len(moves) < max_switches:
            # Respect effective caps: if miner chunks are large, prefer smaller
            # miners that fit within the remaining capacity.
            remaining_cap = effective_caps.get(receiver_id, float("inf"))
            if not math.isinf(remaining_cap):
                remaining_cap = max(
                    0.0,
                    float(remaining_cap)
                    - float(current_allocation.get(receiver_id, 0.0)),
                )
                if remaining_cap <= deadband:
                    break

            picked = pick_idle(remaining_cap)
            source_kind = "idle"
            if picked is None:
                picked = pick_forced(remaining_cap, allow_cooldown=True)
                source_kind = "forced"
            if picked is None:
                picked = pick_soft(remaining_cap, allow_cooldown=False)
                source_kind = "soft"

            if picked is None:
                break

            miner, source_order_id = picked
            reason = (
                MoveReason.HARD_DRAIN
                if source_kind == "forced"
                else MoveReason.MARKET_REBALANCE
            )
            record_move(miner, source_order_id, receiver_id, reason)
            need -= float(miner.hashrate_eh_s)

    # Phase 2: Evacuate remaining forced-drain miners to default (idle) pool
    while len(moves) < max_switches:
        picked = pick_forced(float("inf"), allow_cooldown=True)
        if picked is None:
            break
        miner, source_order_id = picked
        record_move(miner, source_order_id, None, MoveReason.HARD_DRAIN)

    # Phase 3: Shed excess from oversupplied orders to default (damped)
    # Needed when order caps shrink or there are no undersupplied receivers.
    for donor_order_id in soft_donor_order_ids:
        if len(moves) >= max_switches:
            break
        excess = current_allocation.get(donor_order_id, 0.0) - targets.get(
            donor_order_id, 0.0
        )
        if excess <= deadband:
            continue
        shed = min(excess, budget.get(donor_order_id, 0.0))
        if shed <= 0:
            continue
        pool = donors.soft_by_order.get(donor_order_id, [])
        while pool and shed > deadband and len(moves) < max_switches:
            miner = pool.pop(0)
            if miner.worker_ip in moved_ips:
                continue
            if in_cooldown(miner.worker_ip):
                continue
            record_move(miner, donor_order_id, None, MoveReason.MARKET_REBALANCE)
            shed -= float(miner.hashrate_eh_s)

    # ----------------------------------------------------------------------
    # Step 10: Build Diagnostics
    # Compile telemetry data for monitoring and debugging. Includes per-order
    # stats (target, current, budget) and tick-level summary.
    # ----------------------------------------------------------------------
    order_diagnostics: list[OrderDiagnostics] = []
    for order in ctx.snapshot.orders:
        order_id = order.order_id
        eligible = order_id in eligible_orders
        order_diagnostics.append(
            OrderDiagnostics(
                order_id=order_id,
                eligible=eligible,
                ineligible_reason=ctx.ineligible_reason.get(order_id),
                price_btc_eh_day=order.price_btc_eh_day,
                limit_eh_s=order.limit_eh_s,
                balance_btc=order.balance_btc,
                target_eh_s=float(targets.get(order_id, 0.0)),
                current_eh_s=float(current_allocation.get(order_id, 0.0)),
                budget_eh_s=float(budget.get(order_id, 0.0)),
                hard_drain=order_id in hard_drain_ids,
            )
        )

    tick_diagnostics = TickDiagnostics(
        tick_ts=now,
        dt_seconds=float(ctx.tick_elapsed_seconds),
        supply_eh_s=float(supply_eh_s),
        miner_count=len(ctx.snapshot.miners),
        eligible_order_count=len(ranked),
        hard_drain_order_count=len(hard_drain_ids),
        planned_switches=len(moves),
        alpha=float(alpha),
        orders=order_diagnostics,
        events=list(ctx.events),
    )

    return AllocationPlan(moves=moves), tick_diagnostics
