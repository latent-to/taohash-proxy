"""Business rules pipeline for the allocator.

The planner consumes only the prepared context and produces a deterministic
plan for the allocator to execute.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Protocol

from src.marketplace_allocator.types import (
    AllocationSnapshot,
    AllocatorConfig,
    DecisionEvent,
    OrderSnapshot,
)
from src.marketplace_allocator.runtime_state import RuntimeStateSnapshot
from src.marketplace_allocator.pool_health import PoolHealthRecord, PoolHealthStatus


_PROBATION_CAP_ABS_EH_S = Decimal("0.0001")  # 100 TH/s
_PROBATION_CAP_RATE = Decimal("0.10")  # 10% of order


def _probation_cap_eh_s(order_limit_eh_s: Decimal | None) -> Decimal:
    """Compute the reduced effective limit during post-BACKOFF probation."""
    if order_limit_eh_s is None:
        return _PROBATION_CAP_ABS_EH_S
    try:
        cap_by_rate = order_limit_eh_s * _PROBATION_CAP_RATE
    except Exception:
        return _PROBATION_CAP_ABS_EH_S

    effective_cap = min(cap_by_rate, _PROBATION_CAP_ABS_EH_S)
    return effective_cap


@dataclass
class PlanningInput:
    """Mutable context produced by rules and consumed by the planner."""

    snapshot: AllocationSnapshot
    config: AllocatorConfig
    runtime: RuntimeStateSnapshot

    # Timing
    tick_elapsed_seconds: float = 0.0
    tick_elapsed_clamped_seconds: float = 0.0

    # Eligibility
    eligible_orders: list[OrderSnapshot] = field(default_factory=list)
    forced_ineligible_ids: set[str] = field(default_factory=set)
    ineligible_reason: dict[str, str] = field(default_factory=dict)

    # Hard drain
    hard_drain_ids: set[str] = field(default_factory=set)

    # Freeze market moves temporarily
    freeze_market_moves: bool = False

    # Pool health view (computed outside the rules pipeline).
    order_health: dict[str, PoolHealthRecord] = field(default_factory=dict)

    # Events for debugging and telemetry
    events: list[DecisionEvent] = field(default_factory=list)

    def add_event(self, level: str, code: str, message: str, **data) -> None:
        self.events.append(
            DecisionEvent(
                ts=self.snapshot.now,
                level=level,
                code=code,
                message=message,
                data=data or {},
            )
        )


class Rule(Protocol):
    """A small rule that mutates PlanningInput."""

    name: str

    def apply(self, ctx: PlanningInput) -> None: ...


class TickTimingRule:
    """Control how aggressively the planner moves miners this tick.

    Computes elapsed time since last tick and clamps it to interval_seconds.
    This prevents the allocator from moving 100% of hashrate at once after
    a long pause (e.g., restart), keeping rebalancing smooth.

    Reads:
        ctx.snapshot.now - current timestamp
        ctx.runtime.last_tick_ts - when allocator last ran
        ctx.config.poll_seconds - fallback if no last_tick
        ctx.config.interval_seconds - max elapsed time for clamping

    Writes:
        ctx.tick_elapsed_seconds - actual elapsed time since last tick
        ctx.tick_elapsed_clamped_seconds - clamped elapsed (used for damping_factor)
        ctx.events - logs ELAPSED_CLAMPED if clamping occurred
    """

    name = "tick_timing"

    def apply(self, ctx: PlanningInput) -> None:
        now = ctx.snapshot.now
        last = ctx.runtime.last_tick_ts

        if last is None:
            elapsed = float(ctx.config.poll_seconds)
        else:
            elapsed = (now - last).total_seconds()

        # Clamp elapsed time to avoid moving 100% of the delta.
        elapsed_clamped = min(elapsed, float(ctx.config.interval_seconds))
        if elapsed > elapsed_clamped:
            ctx.add_event(
                "info",
                "ELAPSED_CLAMPED",
                "Allocator elapsed time was clamped to protect against mass migration",
                elapsed_seconds=elapsed,
                elapsed_clamped_seconds=elapsed_clamped,
            )

        ctx.tick_elapsed_seconds = elapsed
        ctx.tick_elapsed_clamped_seconds = elapsed_clamped


class EligibilityRule:
    """Determine which orders can receive hashrate.

    Filters snapshot.orders into eligible vs ineligible based on status,
    balance, and optionally hashprice floor. Ineligible orders will be
    drained by the planner (miners moved off them).

    Reads:
        ctx.snapshot.orders - all orders to evaluate
        ctx.snapshot.hashprice_btc_eh_day - current hashprice floor (if any)
        ctx.config.enforce_hashprice_floor - whether to reject below-floor orders

    Writes:
        ctx.eligible_orders - orders that can receive hashrate
        ctx.forced_ineligible_ids - order IDs that must be drained
        ctx.ineligible_reason - human-readable reason per ineligible order
            Possible reasons: "status:<status>", "balance_empty", "below_hashprice_floor"
    """

    name = "eligibility"

    def apply(self, ctx: PlanningInput) -> None:
        eligible: list[OrderSnapshot] = []

        floor: Decimal | None = ctx.snapshot.hashprice_btc_eh_day
        enforce_floor = bool(ctx.config.enforce_hashprice_floor and floor)

        for order in ctx.snapshot.orders:
            if order.status != "active":
                ctx.forced_ineligible_ids.add(order.order_id)
                ctx.ineligible_reason[order.order_id] = f"status:{order.status}"
                continue

            if order.balance_btc <= 0:
                ctx.forced_ineligible_ids.add(order.order_id)
                ctx.ineligible_reason[order.order_id] = "balance_empty"
                continue

            if enforce_floor and order.price_btc_eh_day < floor:  # type: ignore[operator]
                ctx.forced_ineligible_ids.add(order.order_id)
                ctx.ineligible_reason[order.order_id] = "below_hashprice_floor"
                continue

            eligible.append(order)

        ctx.eligible_orders = eligible


class PoolHealthRule:
    """Exclude orders whose upstream pool is unhealthy.

    When an order's pool shows signs of problems (high reject rate, no jobs,
    etc.), it enters BACKOFF state. This rule removes such orders from
    eligibility until the backoff period expires, preventing miners from
    being assigned to broken pools.

    Orders in FAILED state are permanently excluded (no expiry check).

    The pool health state machine is evaluated outside the rule pipeline
    (in the allocator loop) and injected into ctx.order_health before this
    rule runs.

    Reads:
        ctx.config.enable_pool_health - feature toggle (skips if False)
        ctx.order_health - per-order health records with status and backoff times
        ctx.eligible_orders - orders that passed EligibilityRule
        ctx.snapshot.now - current timestamp for backoff expiry check

    Writes:
        ctx.forced_ineligible_ids - adds orders in BACKOFF/FAILED state
        ctx.eligible_orders - removes orders in BACKOFF/FAILED state
        ctx.ineligible_reason - sets "pool_health:<reason>" for affected orders
        ctx.events - logs warning for each affected order
    """

    name = "pool_health"

    def apply(self, ctx: PlanningInput) -> None:
        if not ctx.config.enable_pool_health:
            return

        if not ctx.order_health:
            return

        now_ts = ctx.snapshot.now.timestamp()

        forced: set[str] = set()
        for order in ctx.eligible_orders:
            record = ctx.order_health.get(order.order_id)
            if not record:
                continue

            if record.status == PoolHealthStatus.FAILED:
                forced.add(order.order_id)
                ctx.ineligible_reason[order.order_id] = (
                    f"pool_health_failed:{record.reason or 'max_backoffs'}"
                )
                ctx.add_event(
                    "warning",
                    "POOL_FAILED",
                    "Order is permanently ineligible due to pool health failure",
                    order_id=order.order_id,
                    reason=record.reason,
                )
                continue

            # Backoff state - exclude until timer expires
            if (
                record.status == PoolHealthStatus.BACKOFF
                and record.backoff_until_ts > now_ts
            ):
                forced.add(order.order_id)
                ctx.ineligible_reason[order.order_id] = (
                    f"pool_health:{record.reason or 'backoff'}"
                )
                ctx.add_event(
                    "warning",
                    "POOL_BACKOFF",
                    "Order is temporarily ineligible due to pool health backoff",
                    order_id=order.order_id,
                    reason=record.reason,
                    backoff_until_ts=record.backoff_until_ts,
                )

        if forced:
            ctx.forced_ineligible_ids |= forced
            # Remove from eligible list so they don't consume supply in target computation.
            ctx.eligible_orders = [
                o for o in ctx.eligible_orders if o.order_id not in forced
            ]


# TODO: Revisit the rule again for miner granularity
class PoolPostBackoffProbationCapRule:
    """Apply a probation cap to orders in PROBATION state.

    When an order exits BACKOFF, it enters PROBATION state for careful re-entry.
    During probation we keep the order eligible but reduce its effective limit so
    the planner only allocates a small amount of hashrate until the pool proves
    stable (RECOVERY_TICKS_REQUIRED consecutive GOOD ticks).

    This rule mutates `ctx.eligible_orders` by replacing affected OrderSnapshot
    instances with capped copies (using dataclasses.replace).

    Base cap formula (EH/s):
      min(10% of order.limit_eh_s, 0.0001)
    Where 0.0001 EH/s == 100 TH/s.

    Practical note (miner granularity):
      Orders are allocated in whole-miner units. If the strict probation cap is
      smaller than the smallest miner in the fleet, the planner would be unable
      to allocate *any* hashrate to the probation order, leaving it stuck in
      PROBATION forever.

      To avoid that, we raise the probation cap to the smallest miner's hashrate
      (still never exceeding the order's original limit when it exists). This
      keeps the "canary" semantics (at most one miner) while ensuring the order
      can actually collect samples and recover.
    """

    name = "pool_post_backoff_probation_cap"

    def apply(self, ctx: PlanningInput) -> None:
        if not ctx.config.enable_pool_health:
            return
        if not ctx.order_health or not ctx.eligible_orders:
            return

        # Compute the smallest miner hashrate
        # Used as a guardrail to avoid a probation cap that is too small
        smallest_miner_eh_s: Decimal | None = None
        for miner in ctx.snapshot.miners:
            try:
                hashrate = Decimal(str(miner.hashrate_eh_s))
            except Exception:
                continue
            if hashrate <= 0:
                continue
            if smallest_miner_eh_s is None or hashrate < smallest_miner_eh_s:
                smallest_miner_eh_s = hashrate

        capped_orders: list[OrderSnapshot] = []
        for order in ctx.eligible_orders:
            record = ctx.order_health.get(order.order_id)
            if not record or record.status != PoolHealthStatus.PROBATION:
                capped_orders.append(order)
                continue

            base_cap = _probation_cap_eh_s(order.limit_eh_s)
            effective_cap = base_cap

            if smallest_miner_eh_s is not None and smallest_miner_eh_s > effective_cap:
                if order.limit_eh_s is None:
                    effective_cap = smallest_miner_eh_s
                else:
                    effective_cap = min(order.limit_eh_s, smallest_miner_eh_s)

                if effective_cap > base_cap:
                    ctx.add_event(
                        "info",
                        "POOL_PROBATION_CAP_ADJUSTED_FOR_GRANULARITY",
                        "Raised probation cap so at least one miner can be allocated",
                        order_id=order.order_id,
                        base_probation_cap_eh_s=float(base_cap),
                        adjusted_probation_cap_eh_s=float(effective_cap),
                        smallest_miner_eh_s=str(smallest_miner_eh_s),
                        original_limit_eh_s=(
                            str(order.limit_eh_s)
                            if order.limit_eh_s is not None
                            else None
                        ),
                    )

            # Replace with a capped copy (never increase the original limit)
            if order.limit_eh_s is None or effective_cap < order.limit_eh_s:
                capped_orders.append(replace(order, limit_eh_s=effective_cap))
            else:
                capped_orders.append(order)

            ctx.add_event(
                "info",
                "POOL_PROBATION_CAP",
                "Applied post-backoff probation cap to order",
                order_id=order.order_id,
                probation_cap_eh_s=float(effective_cap),
                base_probation_cap_eh_s=float(base_cap),
                original_limit_eh_s=(
                    str(order.limit_eh_s) if order.limit_eh_s is not None else None
                ),
                good_tick_count=record.good_tick_count,
            )

        ctx.eligible_orders = capped_orders


class HardDrainRule:
    """Compute the set of orders that need immediate miner evacuation.

    An order needs hard-draining if miners are currently assigned to it but
    it's no longer eligible. This can happen when:
      - Order status changed (cancelled, paused, etc.)
      - Balance depleted
      - Pool went into BACKOFF
      - Order dropped below hashprice floor

    The planner uses hard_drain_ids to prioritize moving miners off these
    orders before doing normal market rebalancing.

    Reads:
        ctx.eligible_orders - orders that can receive hashrate (from prior rules)
        ctx.forced_ineligible_ids - orders explicitly marked ineligible
        ctx.snapshot.miners - current miner assignments (pool_label)
        ctx.config.default_labels - labels that are not market orders (e.g., "normal")

    Writes:
        ctx.hard_drain_ids - union of (currently_mined - eligible) and forced_ineligible
        ctx.events - logs HARD_DRAIN_SET info event listing affected order IDs
    """

    name = "hard_drain"

    def apply(self, ctx: PlanningInput) -> None:
        eligible_ids = {o.order_id for o in ctx.eligible_orders}
        currently_mined: set[str] = set()

        # Identify orders currently being mined
        for miner in ctx.snapshot.miners:
            label = miner.pool_label
            if label in ctx.config.default_labels:
                continue
            currently_mined.add(label)

        # Identify orders that are currently being mined but not eligible
        hard_drain = (currently_mined - eligible_ids) | set(ctx.forced_ineligible_ids)
        ctx.hard_drain_ids = hard_drain

        if hard_drain:
            ctx.add_event(
                "info",
                "HARD_DRAIN_SET",
                "Computed hard-drain set",
                hard_drain_ids=sorted(hard_drain),
            )


def default_rules() -> list[Rule]:
    """Return the default rules pipeline (ordered)."""

    return [
        TickTimingRule(),
        EligibilityRule(),
        PoolHealthRule(),
        PoolPostBackoffProbationCapRule(),
        HardDrainRule(),
    ]
