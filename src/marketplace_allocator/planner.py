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

    now = ctx.snapshot.now
    ranked = _rank_orders(ctx)
    eligible_orders = {order.order_id for order in ranked}

    # ----------------------------------------------------------------------
    # Sum all miner hashrates to know how much capacity we have to allocate.
    # ----------------------------------------------------------------------
    supply_eh_s = float(sum(miner.hashrate_eh_s for miner in ctx.snapshot.miners))

    # ----------------------------------------------------------------------
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
    # Orders flagged by rules (ineligible, unhealthy pools) must be emptied.
    # Set their target to 0 so all their miners become donors.
    # ----------------------------------------------------------------------
    hard_drain_ids = set(ctx.hard_drain_ids)
    for order_id in hard_drain_ids:
        targets[order_id] = 0.0

    # ----------------------------------------------------------------------
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
