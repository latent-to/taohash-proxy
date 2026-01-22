"""
TaoHash Marketplace Allocator Planner.

This module implements the core marketplace algorithm. It consumes a
PlanningInput (prepared by the rules pipeline) and produces:

  * AllocationPlan: a list of miner moves
  * TickDiagnostics: a compact explanation of targets, budgets, and moves

Key Algorithm Properties:
  * Price-time ranking: highest-paying orders fill first, ties broken by queue time
  * Greedy fill: each order receives hashrate up to its cap before moving to the next
  * Exponential damping: market rebalances happen gradually to avoid thrashing
  * Hard-drain: miners on ineligible/unknown orders are evacuated immediately

Entry Point:
  plan_tick(ctx: PlanningInput) -> (AllocationPlan, TickDiagnostics)

Called By:
  order_allocator.py -> _run_once() invokes plan_tick() after rules are applied
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.marketplace_allocator.types import MinerSnapshot
from src.marketplace_allocator.rules import PlanningInput
from src.marketplace_allocator.types import (
    AllocationPlan,
    MoveReason,
    OrderDiagnostics,
    PlanMove,
    TickDiagnostics,
)

_FIT_EPS = 1e-12  # Tolerance for floating-point hashrate comparisons


@dataclass
class _AvailableMiners:
    """Categorized miners available to donate hashrate to receivers.

    Miners are drawn in priority order: forced -> idle -> soft.

    Attributes:
        idle: Miners on default labels (not assigned to any order). First choice
              since moving them doesn't disrupt existing allocations.
        forced: Miners on hard-drain orders (ineligible/unhealthy). Must be moved
                regardless of cooldown - these orders cannot receive hashrate.
        soft_by_order: Miners on oversupplied eligible orders, keyed by order_id.
                       Lowest-priority orders moved first to minimize disruption
                       to high-paying orders.
    """

    idle: list[MinerSnapshot]
    forced: list[MinerSnapshot]
    soft_by_order: dict[str, list[MinerSnapshot]]


# Utility functions


def _compute_alpha(rate: float, dt_seconds: float, interval_seconds: float) -> float:
    """Compute exponential smoothing coefficient for movement damping.

    Calculates the damping factor (damping_factor) used to limit how much hashrate can
    move per tick, preventing thrashing. The formula ensures that over the
    specified interval, approximately 'rate' fraction of the delta will move.

    Formula: damping_factor = 1 - (1 - rate)^(dt/interval)

    Args:
        rate: Target rebalance rate (0-1), fraction of delta to move per interval
        dt_seconds: Time elapsed since last tick (for computing damping)
        interval_seconds: Reference interval for the rebalance rate

    Returns:
        Damping coefficient (0-1). Returns 1.0 if interval <= 0 (no damping),
        0.0 if rate <= 0 (no movement), or 1.0 if rate >= 1 (full movement).
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


def _rank_orders(ctx: PlanningInput) -> list:
    """Rank eligible orders by priority for greedy allocation.

    Sorts orders by price (descending), then by queue time (ascending).
    Highest-paying orders fill first, with earlier orders breaking ties.
    This ensures optimal revenue allocation.

    Args:
        ctx: Planning context containing eligible orders

    Returns:
        List of OrderSnapshot sorted by priority (highest first)
    """
    return sorted(
        ctx.eligible_orders,
        key=lambda order: (-order.price_btc_eh_day, order.queue_position_time),
    )


def _is_default_label(ctx: PlanningInput, label: str) -> bool:
    """Check if a pool label is a default/idle label.

    Default labels (e.g., "normal", "high_diff") indicate miners not assigned
    to any marketplace order. These miners are idle and can be allocated to
    orders without disrupting existing assignments.

    Args:
        ctx: Planning context containing default label configuration
        label: Pool label to check

    Returns:
        True if label is a default label, False otherwise
    """
    return label in ctx.config.default_labels


def _order_id_from_label(ctx: PlanningInput, label: str) -> Optional[str]:
    """Extract order_id from a pool label. Returns None if it's a default label."""
    if _is_default_label(ctx, label):
        return None
    return label


def _fits(miner: MinerSnapshot, max_hashrate: float) -> bool:
    """Check if miner's hashrate fits within remaining order capacity.

    Uses floating-point tolerance (_FIT_EPS) to handle precision issues when
    comparing hashrate values. Prevents rejecting miners due to tiny rounding
    differences.

    Args:
        miner: Miner snapshot with hashrate to check
        max_hashrate: Maximum allowed hashrate (can be infinity for no limit)

    Returns:
        True if miner fits (hashrate <= max_hashrate + tolerance), False otherwise.
        Always returns True if max_hashrate is infinity.
    """
    if math.isinf(max_hashrate):
        return True
    return float(miner.hashrate_eh_s) <= float(max_hashrate) + _FIT_EPS


def _compute_effective_deadband(
    config_deadband: float,
    miners: list[MinerSnapshot],
) -> float:
    """Compute a deadband consistent with miner chunk sizes.

    We move discrete miner chunks (groups). If the planner chases deltas smaller
    than realistic chunk sizes, it can get stuck retrying the same tiny residual
    every tick. This adjusts the deadband upward based on the smallest positive
    miner chunk.

    Behavior:
        effective_deadband = max(config_deadband, 0.5 * min_positive_chunk)

    Args:
        config_deadband: configured rebalance threshold (EH/s)
        miners: list of miner snapshots for this tick

    Returns:
        effective deadband (EH/s)
    """
    min_chunk: Optional[float] = None
    for miner in miners:
        try:
            hashrate = float(miner.hashrate_eh_s)
        except Exception:
            continue
        if hashrate <= 0:
            continue
        min_chunk = hashrate if min_chunk is None else min(min_chunk, hashrate)

    if min_chunk is None:
        return float(config_deadband)

    return max(float(config_deadband), 0.5 * float(min_chunk))


# Data computation functions


def _compute_supply(miners: list[MinerSnapshot]) -> float:
    """Compute total hashrate supply from all connected miners.

    Sums the hashrate of all miners to determine total capacity available for
    allocation to marketplace orders. This is the maximum hashrate that can be
    distributed across orders.

    Args:
        miners: List of all miner snapshots

    Returns:
        Total supply in EH/s (sum of all miner hashrates)
    """
    return float(sum(miner.hashrate_eh_s for miner in miners))


def _compute_targets(
    ranked_orders: list,
    supply_eh_s: float,
    overshoot_pct: float,
) -> tuple[dict[str, float], dict[str, float]]:
    """Compute ideal allocation targets via greedy fill algorithm.

    Allocates hashrate to orders in priority order (highest price first),
    giving each order as much as it can take up to its cap before moving to
    the next. Ensures highest-paying orders are fully satisfied first.

    Effective caps include overshoot allowance to prevent rejecting miners
    whose hashrate slightly exceeds remaining capacity due to granularity.

    Args:
        ranked_orders: Orders sorted by priority (highest price first)
        supply_eh_s: Total hashrate available to allocate
        overshoot_pct: Percentage allowance for cap overshoot (e.g., 0.15 = 15%)

    Returns:
        Tuple of (targets, effective_order_caps):
            - targets: Dict mapping order_id -> target hashrate allocation
            - effective_order_caps: Dict mapping order_id -> effective cap (with overshoot)
    """
    all_target_allocations: dict[str, float] = {}
    effective_order_caps: dict[str, float] = {}
    remaining_supply = supply_eh_s

    for order in ranked_orders:
        if remaining_supply <= 0.0:
            all_target_allocations[order.order_id] = 0.0
            continue

        max_order_cap = float("inf")
        if order.limit_eh_s is not None:
            try:
                max_order_cap = float(order.limit_eh_s)
            except Exception:
                max_order_cap = float("inf")

            # Allow the order to exceed its limit by the overshoot percentage
            max_order_cap = max_order_cap * (1.0 + float(overshoot_pct))

        effective_order_caps[order.order_id] = max_order_cap

        target_allocation = min(max_order_cap, remaining_supply)
        if target_allocation < 0:
            target_allocation = 0.0

        all_target_allocations[order.order_id] = target_allocation
        remaining_supply -= target_allocation

    return all_target_allocations, effective_order_caps


def _compute_current_allocation(
    ctx: PlanningInput,
    miners: list[MinerSnapshot],
) -> tuple[
    dict[str, float], dict[str, list[MinerSnapshot]], list[MinerSnapshot], set[str]
]:
    """Compute current hashrate allocation from miner pool labels.

    Groups miners by their current pool_label to determine where hashrate is
    currently allocated. This enables computing deltas (target - current) for
    each order to determine how much hashrate needs to move.

    Args:
        ctx: Planning context for label parsing
        miners: List of all miner snapshots

    Returns:
        Tuple of:
            - current_allocation: Dict mapping order_id -> current hashrate (EH/s)
            - miners_by_order: Dict mapping order_id -> list of miners on that order
            - idle_miners: List of miners on default labels (not assigned)
            - currently_mined: Set of order_ids currently being mined
    """
    current_allocation: dict[str, float] = {}
    miners_by_order: dict[str, list[MinerSnapshot]] = {}
    idle_miners: list[MinerSnapshot] = []
    currently_mined: set[str] = set()

    for miner in miners:
        order_id = _order_id_from_label(ctx, miner.pool_label)
        if order_id is None:
            idle_miners.append(miner)
            continue

        currently_mined.add(order_id)
        current_allocation[order_id] = current_allocation.get(order_id, 0.0) + float(
            miner.hashrate_eh_s
        )
        miners_by_order.setdefault(order_id, []).append(miner)

    return current_allocation, miners_by_order, idle_miners, currently_mined


def _apply_hard_drain(
    targets: dict[str, float],
    hard_drain_ids: set[str],
) -> dict[str, float]:
    """Set target=0 for orders that must be evacuated immediately.

    Orders flagged by rules (ineligible, unhealthy pools, etc.) cannot receive
    hashrate and must be drained. Setting their target to 0 ensures all miners
    on these orders become donors and are moved elsewhere.

    Args:
        targets: Current target allocation dict (will be modified)
        hard_drain_ids: Set of order IDs that must be evacuated

    Returns:
        Updated targets dict with hard-drain orders set to 0.0
    """
    updated_targets = dict(targets)
    for order_id in hard_drain_ids:
        updated_targets[order_id] = 0.0
    return updated_targets


def _compute_budgets(
    ranked_orders: list,
    targets: dict[str, float],
    current_allocation: dict[str, float],
    hard_drain_ids: set[str],
    damping_factor: float,
    deadband: float,
) -> dict[str, float]:
    """Compute movement budgets to limit hashrate movement per tick.

    Limits how much hashrate can move per tick to avoid thrashing. Market
    moves use exponential damping (damping_factor * delta), while hard-drain moves
    get full budget (must evacuate immediately). Movements below deadband
    threshold are ignored to prevent micro-adjustments.

    Args:
        ranked_orders: Eligible orders sorted by priority
        targets: Target allocation per order
        current_allocation: Current allocation per order
        hard_drain_ids: Orders that must be evacuated (get full budget)
        damping_factor: Damping coefficient (0-1) from exponential smoothing
        deadband: Minimum delta threshold to trigger movement (EH/s)

    Returns:
        Dict mapping order_id -> movement budget (EH/s). Budget is 0.0 if
        delta is below deadband, damping_factor * abs(delta) for eligible orders,
        or full current_allocation for hard-drain orders.
    """
    budget: dict[str, float] = {}

    # Eligible orders: damped budget
    for order in ranked_orders:
        order_id = order.order_id
        delta = targets.get(order_id, 0.0) - current_allocation.get(order_id, 0.0)
        if abs(delta) <= deadband:
            budget[order_id] = 0.0
        else:
            budget[order_id] = damping_factor * abs(delta)

    # Hard drain orders: full drain budget
    for order_id in hard_drain_ids:
        budget[order_id] = current_allocation.get(order_id, 0.0)

    return budget


def _build_donor_pools(
    ctx: PlanningInput,
    idle_miners: list[MinerSnapshot],
    miners_by_order: dict[str, list[MinerSnapshot]],
    hard_drain_ids: set[str],
    ranked_orders: list,
    eligible_orders: set[str],
    targets: dict[str, float],
    current_allocation: dict[str, float],
    deadband: float,
) -> tuple[_AvailableMiners, list[str]]:
    """Build categorized donor pools for miner allocation.

    Categorizes miners into three pools based on their current assignment:
    - Idle: Miners on default labels (not assigned to orders) - first choice
    - Forced: Miners on hard-drain orders (must evacuate immediately)
    - Soft: Miners on oversupplied eligible orders (can donate excess)

    Miners are sorted by hashrate (largest first) within each pool. Soft donors
    are prioritized by order rank (lowest-priority orders donate first) to
    minimize disruption to high-paying customers.

    Args:
        ctx: Planning context for label parsing
        idle_miners: Miners currently on default labels
        miners_by_order: Miners grouped by their current order assignment
        hard_drain_ids: Orders that must be evacuated
        ranked_orders: Orders sorted by priority
        eligible_orders: Set of eligible order IDs
        targets: Target allocation per order
        current_allocation: Current allocation per order
        deadband: Threshold for considering orders oversupplied

    Returns:
        Tuple of:
            - donors: _AvailableMiners with categorized pools
            - soft_donor_order_ids: List of oversupplied order IDs (lowest priority first)
    """
    # Idle donors: sorted by hashrate (largest first)
    donors_idle = sorted(idle_miners, key=lambda miner: -miner.hashrate_eh_s)

    # Forced donors: miners on hard-drain orders
    donors_forced: list[MinerSnapshot] = []
    for order_id in hard_drain_ids:
        donors_forced.extend(miners_by_order.get(order_id, []))
    donors_forced.sort(key=lambda miner: -miner.hashrate_eh_s)

    # Soft donors: eligible orders that are oversupplied (current > target)
    # Priority is determined by the order's position in the ranked list.
    rank_index = {order.order_id: rank for rank, order in enumerate(ranked_orders)}
    soft_donor_order_ids = [
        order_id
        for order_id in eligible_orders
        if current_allocation.get(order_id, 0.0) > targets.get(order_id, 0.0) + deadband
    ]
    soft_donor_order_ids.sort(
        key=lambda order_id: rank_index.get(order_id, 10**9), reverse=True
    )

    donors_soft_by_order: dict[str, list[MinerSnapshot]] = {}
    for order_id in soft_donor_order_ids:
        order_miners = list(miners_by_order.get(order_id, []))
        order_miners.sort(key=lambda miner: -miner.hashrate_eh_s)
        donors_soft_by_order[order_id] = order_miners

    donors = _AvailableMiners(
        idle=donors_idle, forced=donors_forced, soft_by_order=donors_soft_by_order
    )

    return donors, soft_donor_order_ids


def _identify_receivers(
    ranked_orders: list,
    targets: dict[str, float],
    current_allocation: dict[str, float],
    deadband: float,
) -> list[str]:
    """Identify orders that need more hashrate (receivers).

    Finds orders where target allocation exceeds current allocation by more than
    the deadband threshold. Returns them in priority order (highest price first)
    so that highest-paying orders receive miners before lower-paying ones.

    Args:
        ranked_orders: Orders sorted by priority (highest price first)
        targets: Target allocation per order
        current_allocation: Current allocation per order
        deadband: Minimum delta threshold to consider an order undersupplied

    Returns:
        List of order IDs that need hashrate, in priority order
    """
    receivers: list[str] = []
    for order in ranked_orders:
        order_id = order.order_id
        need_total = targets.get(order_id, 0.0) - current_allocation.get(order_id, 0.0)
        if need_total > deadband:
            receivers.append(order_id)
    return receivers


class MinerPicker:
    """Initialize miner picker for selecting miners from donor pools.

    Args:
        donors: Categorized donor pools (idle, forced, soft)
        ctx: Planning context for cooldown checks and label parsing
        moved_ips: Set of worker IPs already moved this tick (to avoid duplicates)
        now: Current timestamp for cooldown calculations
        soft_donor_order_ids: List of oversupplied order IDs (lowest priority first)
    """

    def __init__(
        self,
        donors: _AvailableMiners,
        ctx: PlanningInput,
        moved_ips: set[str],
        now: datetime,
        soft_donor_order_ids: list[str],
    ):
        self.donors = donors
        self.ctx = ctx
        self.moved_ips = moved_ips
        self.now = now
        self.soft_donor_order_ids = soft_donor_order_ids

    def _in_cooldown(self, worker_ip: str) -> bool:
        """Check if a miner is in cooldown period.

        Miners that were recently moved should not be moved again immediately to
        prevent thrashing. Cooldown period is configured in AllocatorConfig.

        Args:
            worker_ip: Miner worker IP to check

        Returns:
            True if miner was moved within the cooldown period, False otherwise
        """
        return self.ctx.runtime.miner_in_cooldown(
            worker_ip, self.now, self.ctx.config.miner_cooldown_seconds
        )

    def pick_idle(self, max_hashrate: float) -> Optional[tuple[MinerSnapshot, None]]:
        """Pick an idle miner that fits within the capacity limit.

        Selects a miner from the idle pool (default labels) that hasn't been moved
        this tick and isn't in cooldown. Prefers miners that fit within max_hashrate
        to avoid rejecting large miners that could be used for larger orders.

        Args:
            max_hashrate: Maximum hashrate that can be allocated (infinity = no limit)

        Returns:
            Tuple of (miner, None) if found, None otherwise. Source order is None
            since idle miners aren't assigned to any order.
        """
        for idx, miner in enumerate(self.donors.idle):
            if miner.worker_ip in self.moved_ips:
                continue
            if self._in_cooldown(miner.worker_ip):
                continue
            if not _fits(miner, max_hashrate):
                continue
            self.donors.idle.pop(idx)
            return miner, None
        return None

    def pick_forced(
        self, max_hashrate: float, allow_cooldown: bool
    ) -> Optional[tuple[MinerSnapshot, Optional[str]]]:
        """Pick a miner from forced-drain orders (must evacuate).

        Selects a miner from orders that must be evacuated immediately (ineligible,
        unhealthy pools). These miners can bypass cooldown when allow_cooldown=True
        because they MUST be moved off bad orders regardless of recent movement.

        Args:
            max_hashrate: Maximum hashrate that can be allocated
            allow_cooldown: If True, bypasses cooldown check (forced evacuation)

        Returns:
            Tuple of (miner, source_order_id) if found, None otherwise
        """
        for idx, miner in enumerate(self.donors.forced):
            if miner.worker_ip in self.moved_ips:
                continue
            if (not allow_cooldown) and self._in_cooldown(miner.worker_ip):
                continue
            if not _fits(miner, max_hashrate):
                continue
            self.donors.forced.pop(idx)
            source_order_id = _order_id_from_label(self.ctx, miner.pool_label)
            return miner, source_order_id
        return None

    def pick_soft(
        self, max_hashrate: float, allow_cooldown: bool
    ) -> Optional[tuple[MinerSnapshot, str]]:
        """Pick a miner from an oversupplied eligible order.

        Selects a miner from orders that have excess hashrate (current > target).
        Iterates through soft donor orders in priority order (lowest-priority first)
        to minimize disruption to high-paying customers.

        Args:
            max_hashrate: Maximum hashrate that can be allocated
            allow_cooldown: If True, bypasses cooldown check (typically False for
                        market rebalances to respect cooldowns)

        Returns:
            Tuple of (miner, source_order_id) if found, None otherwise
        """
        for donor_order_id in self.soft_donor_order_ids:
            pool = self.donors.soft_by_order.get(donor_order_id)
            if not pool:
                continue
            for idx, miner in enumerate(pool):
                if miner.worker_ip in self.moved_ips:
                    continue
                if (not allow_cooldown) and self._in_cooldown(miner.worker_ip):
                    continue
                if not _fits(miner, max_hashrate):
                    continue
                pool.pop(idx)
                return miner, donor_order_id
        return None


class MoveRecorder:
    """Initialize move recorder for tracking planned moves.

    Maintains local allocation accounting that gets updated as moves are recorded,
    allowing the planner to track remaining capacity and avoid double-moving miners.

    Args:
        current_allocation: Current allocation dict (will be updated as moves recorded)
        moved_ips: Set of worker IPs already moved (will be updated as moves recorded)
    """

    def __init__(
        self,
        current_allocation: dict[str, float],
        moved_ips: set[str],
    ):
        self.current_allocation = current_allocation
        self.moved_ips = moved_ips
        self.moves: list[PlanMove] = []

    def record_move(
        self,
        miner: MinerSnapshot,
        source_order_id: Optional[str],
        dest_order_id: Optional[str],
        reason: MoveReason,
    ) -> None:
        """Record a planned move and update local allocation accounting.

        Adds a move to the plan and updates the local allocation tracking to reflect
        the hashrate transfer. This allows subsequent moves to see updated allocations
        and prevents double-moving the same miner.

        Args:
            miner: Miner being moved
            source_order_id: Order miner is moving from (None if idle)
            dest_order_id: Order miner is moving to (None if moving to idle/default)
            reason: Reason for the move (HARD_DRAIN or MARKET_REBALANCE)
        """
        self.moved_ips.add(miner.worker_ip)
        self.moves.append(
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
            self.current_allocation[source_order_id] = max(
                0.0,
                self.current_allocation.get(source_order_id, 0.0)
                - float(miner.hashrate_eh_s),
            )
        if dest_order_id:
            self.current_allocation[dest_order_id] = self.current_allocation.get(
                dest_order_id, 0.0
            ) + float(miner.hashrate_eh_s)


def _fill_receivers(
    ctx: PlanningInput,
    receivers: list[str],
    targets: dict[str, float],
    current_allocation: dict[str, float],
    budget: dict[str, float],
    effective_order_caps: dict[str, float],
    deadband: float,
    max_switches: int,
    picker: MinerPicker,
    recorder: MoveRecorder,
) -> None:
    """Phase 1: Fill undersupplied orders by drawing miners from donor pools.

    Allocates miners to orders that need more hashrate (receivers) by drawing
    from donor pools in priority order: forced -> idle -> soft.
    """
    for receiver_id in receivers:
        if len(recorder.moves) >= max_switches:
            break

        # Receiver must actually need hashrate (target > current)
        initial_need_total = targets.get(receiver_id, 0.0) - current_allocation.get(
            receiver_id, 0.0
        )
        if initial_need_total <= deadband:
            continue

        market_budget_total = float(budget.get(receiver_id, 0.0))
        market_used = 0.0

        while len(recorder.moves) < max_switches:
            # Recompute need_total after any move (forced or market) updates local accounting
            need_total = targets.get(receiver_id, 0.0) - current_allocation.get(
                receiver_id, 0.0
            )
            if need_total <= deadband:
                break

            # Respect effective caps
            remaining_cap = effective_order_caps.get(receiver_id, float("inf"))
            if not math.isinf(remaining_cap):
                remaining_cap = max(
                    0.0,
                    float(remaining_cap)
                    - float(current_allocation.get(receiver_id, 0.0)),
                )
                if remaining_cap <= deadband:
                    break

            # 1) Forced donors first: cap-limited, NOT budget-limited
            picked_forced = picker.pick_forced(remaining_cap, allow_cooldown=True)
            if picked_forced is not None:
                miner, source_order_id = picked_forced
                recorder.record_move(
                    miner, source_order_id, receiver_id, MoveReason.HARD_DRAIN
                )
                continue

            # 2) Market donors: budget-limited AND cap-limited
            remaining_market_budget = max(0.0, market_budget_total - market_used)
            need_market = min(float(need_total), float(remaining_market_budget))
            if need_market <= deadband:
                break

            selection_bound = float(need_market)
            if not math.isinf(remaining_cap):
                selection_bound = min(selection_bound, float(remaining_cap))

            picked = picker.pick_idle(selection_bound)
            if picked is None:
                picked = picker.pick_soft(selection_bound, allow_cooldown=False)

            if picked is None:
                break

            miner, source_order_id = picked
            recorder.record_move(
                miner, source_order_id, receiver_id, MoveReason.MARKET_REBALANCE
            )
            market_used += float(miner.hashrate_eh_s)


def _evacuate_forced(
    max_switches: int,
    picker: MinerPicker,
    recorder: MoveRecorder,
) -> None:
    """Phase 2: Evacuate remaining forced-drain miners to default pool.

    Moves any remaining miners from hard-drain orders (ineligible, unhealthy)
    to the default/idle pool. These miners must be evacuated immediately
    regardless of cooldown, as their orders cannot receive hashrate.
    """
    while len(recorder.moves) < max_switches:
        picked = picker.pick_forced(float("inf"), allow_cooldown=True)
        if picked is None:
            break
        miner, source_order_id = picked
        recorder.record_move(miner, source_order_id, None, MoveReason.HARD_DRAIN)


def _shed_excess(
    ctx: PlanningInput,
    soft_donor_order_ids: list[str],
    targets: dict[str, float],
    current_allocation: dict[str, float],
    budget: dict[str, float],
    donors: _AvailableMiners,
    deadband: float,
    max_switches: int,
    moved_ips: set[str],
    now: datetime,
    recorder: MoveRecorder,
) -> None:
    """Phase 3: Shed excess hashrate from oversupplied orders to default pool.

    Removes excess hashrate from orders that are oversupplied (current > target).
    Needed when order caps shrink or there are no undersupplied receivers to
    fill. Uses damped budgets to prevent sudden changes.

    Budget discipline (B-fix):
      - Only shed miner chunks that fit within remaining shed budget.
      - Because we iterate smallest-first, if the smallest can't fit, stop.
    """
    for donor_order_id in soft_donor_order_ids:
        if len(recorder.moves) >= max_switches:
            break

        excess = current_allocation.get(donor_order_id, 0.0) - targets.get(
            donor_order_id, 0.0
        )
        if excess <= deadband:
            continue

        shed = min(float(excess), float(budget.get(donor_order_id, 0.0)))
        if shed <= deadband:
            continue

        pool = donors.soft_by_order.get(donor_order_id, [])
        # Get smallest miners first (prevent overshooting excess)
        pool = list(reversed(pool))

        while pool and shed > deadband and len(recorder.moves) < max_switches:
            miner = pool.pop(0)

            if miner.worker_ip in moved_ips:
                continue
            if ctx.runtime.miner_in_cooldown(
                miner.worker_ip, now, ctx.config.miner_cooldown_seconds
            ):
                continue

            # STRICT budget fit: don't overshoot shed
            if not _fits(miner, shed):
                break

            recorder.record_move(
                miner, donor_order_id, None, MoveReason.MARKET_REBALANCE
            )
            shed -= float(miner.hashrate_eh_s)


# Diagnostics


def _build_diagnostics(
    ctx: PlanningInput,
    now: datetime,
    supply_eh_s: float,
    ranked_orders: list,
    eligible_orders: set[str],
    targets: dict[str, float],
    current_allocation: dict[str, float],
    budget: dict[str, float],
    hard_drain_ids: set[str],
    damping_factor: float,
    moves: list[PlanMove],
) -> TickDiagnostics:
    """Build diagnostics and telemetry data for monitoring.

    Compiles comprehensive diagnostics including per-order statistics (target,
    current, budget, eligibility) and tick-level summary (supply, moves, damping_factor).
    Published to Redis for real-time monitoring and debugging.
    """
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

    return TickDiagnostics(
        tick_ts=now,
        dt_seconds=float(ctx.tick_elapsed_seconds),
        supply_eh_s=float(supply_eh_s),
        miner_count=len(ctx.snapshot.miners),
        eligible_order_count=len(ranked_orders),
        hard_drain_order_count=len(hard_drain_ids),
        planned_switches=len(moves),
        damping_factor=float(damping_factor),
        orders=order_diagnostics,
        events=list(ctx.events),
    )


# Main planning function


def plan_tick(
    planning_context: PlanningInput,
) -> tuple[AllocationPlan, TickDiagnostics]:
    """Plan one allocator tick - core allocation algorithm.

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
        7. Build donor pools: forced -> idle -> soft (in priority order)
        8. Identify receivers (orders where target > current)
        9. Execute moves:
           a) Fill receivers by drawing from donor pools
           b) Evacuate any remaining forced-drain miners to default
           c) Shed excess from oversupplied orders to default
        10. Build diagnostics for telemetry

    Args:
        planning_context: PlanningContext with eligibility/health rules already
                          applied by the rules pipeline

    Returns:
        Tuple of:
            - AllocationPlan: List of moves to execute (miner switches)
            - TickDiagnostics: Telemetry data for monitoring/debugging

    Note:
        This function is pure and deterministic. All side effects (DB/Redis writes)
        happen in the executor.
    """
    now = planning_context.snapshot.now
    ranked = _rank_orders(planning_context)
    eligible_orders = {order.order_id for order in ranked}

    # Step 1-2: Total Supply
    supply_eh_s = _compute_supply(planning_context.snapshot.miners)

    # Step 3: Compute Ideal Targets (Greedy Fill)
    targets, effective_order_caps = _compute_targets(
        ranked, supply_eh_s, planning_context.config.overshoot_pct
    )

    # Step 4: Compute Current Allocation
    (
        current_allocation,
        miners_by_order,
        idle_miners,
        currently_mined,
    ) = _compute_current_allocation(planning_context, planning_context.snapshot.miners)

    # Step 5: Hard Drain Orders
    hard_drain_ids = set(planning_context.hard_drain_ids)
    targets = _apply_hard_drain(targets, hard_drain_ids)

    # Step 6: Movement Budgets
    damping_factor = _compute_alpha(
        planning_context.config.rebalance_rate,
        planning_context.tick_elapsed_clamped_seconds,
        float(planning_context.config.interval_seconds),
    )
    if planning_context.freeze_market_moves:
        damping_factor = 0.0

    config_deadband = float(planning_context.config.rebalance_threshold_eh_s)
    deadband = _compute_effective_deadband(
        config_deadband, planning_context.snapshot.miners
    )

    budget = _compute_budgets(
        ranked,
        targets,
        current_allocation,
        hard_drain_ids,
        damping_factor,
        deadband,
    )

    # Step 7: Build Donor Pools
    donors, soft_donor_order_ids = _build_donor_pools(
        planning_context,
        idle_miners,
        miners_by_order,
        hard_drain_ids,
        ranked,
        eligible_orders,
        targets,
        current_allocation,
        deadband,
    )

    # Step 8: Identify Receivers
    receivers = _identify_receivers(ranked, targets, current_allocation, deadband)

    # Step 9: Execute Moves
    moved_ips: set[str] = set()
    recorder = MoveRecorder(current_allocation, moved_ips)
    picker = MinerPicker(
        donors,
        planning_context,
        moved_ips,
        now,
        soft_donor_order_ids,
    )

    max_switches = int(planning_context.config.max_switches_per_tick)

    # Phase 1: Fill receivers
    _fill_receivers(
        planning_context,
        receivers,
        targets,
        current_allocation,
        budget,
        effective_order_caps,
        deadband,
        max_switches,
        picker,
        recorder,
    )

    # Phase 2: Evacuate forced
    _evacuate_forced(max_switches, picker, recorder)

    # Phase 3: Shed excess
    _shed_excess(
        planning_context,
        soft_donor_order_ids,
        targets,
        current_allocation,
        budget,
        donors,
        deadband,
        max_switches,
        moved_ips,
        now,
        recorder,
    )

    # Step 10: Build Diagnostics
    diagnostics = _build_diagnostics(
        planning_context,
        now,
        supply_eh_s,
        ranked,
        eligible_orders,
        targets,
        current_allocation,
        budget,
        hard_drain_ids,
        damping_factor,
        recorder.moves,
    )

    return AllocationPlan(moves=recorder.moves), diagnostics
