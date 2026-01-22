"""Business rules pipeline for the allocator.

The planner consumes only the prepared context and produces a deterministic
plan for the allocator to execute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from src.marketplace_allocator.types import (
    AllocationSnapshot,
    AllocatorConfig,
    DecisionEvent,
    OrderSnapshot,
)
from src.marketplace_allocator.runtime_state import RuntimeStateView
from src.marketplace_allocator.pool_health import PoolHealthRecord, PoolHealthState


@dataclass
class PlanningContext:
    """Mutable context produced by rules and consumed by the planner."""

    snapshot: AllocationSnapshot
    config: AllocatorConfig
    runtime: RuntimeStateView

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
    """A small rule that mutates PlanningContext."""

    name: str

    def apply(self, ctx: PlanningContext) -> None: ...


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
        ctx.tick_elapsed_clamped_seconds - clamped elapsed (used for alpha)
        ctx.events - logs ELAPSED_CLAMPED if clamping occurred
    """

    name = "tick_timing"

    def apply(self, ctx: PlanningContext) -> None:
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

    def apply(self, ctx: PlanningContext) -> None:
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
    """Temporarily exclude orders whose upstream pool is unhealthy.

    When an order's pool shows signs of problems (high reject rate, no jobs,
    etc.), it enters BACKOFF state. This rule removes such orders from
    eligibility until the backoff period expires, preventing miners from
    being assigned to broken pools.

    The pool health state machine is evaluated outside the rule pipeline
    (in the allocator loop) and injected into ctx.order_health before this
    rule runs.

    Reads:
        ctx.config.enable_pool_health - feature toggle (skips if False)
        ctx.order_health - per-order health records with status and backoff times
        ctx.eligible_orders - orders that passed EligibilityRule
        ctx.snapshot.now - current timestamp for backoff expiry check

    Writes:
        ctx.forced_ineligible_ids - adds orders in BACKOFF state
        ctx.eligible_orders - removes orders in BACKOFF state
        ctx.ineligible_reason - sets "pool_health:<reason>" for affected orders
        ctx.events - logs POOL_BACKOFF warning for each affected order
    """

    name = "pool_health"

    def apply(self, ctx: PlanningContext) -> None:
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

            if (
                record.status == PoolHealthState.BACKOFF
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

    def apply(self, ctx: PlanningContext) -> None:
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
        HardDrainRule(),
    ]
