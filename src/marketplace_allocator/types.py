"""
Allocator datatypes.

These dataclasses are used to:
  * build a read-only snapshot of the current state (orders + miners + hashprice)
  * compute a deterministic plan (miner moves + order actions)
  * export diagnostics for logs / dashboards
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


DEFAULT_LABELS = {"normal", "high_diff", "unknown"}


def gh_to_eh(gh_s: float) -> float:
    """Convert GH/s to EH/s."""

    return float(gh_s) / 1e9


class MoveReason(str, Enum):
    """Why the allocator decided to move a miner."""

    MARKET_REBALANCE = "MARKET_REBALANCE"
    HARD_DRAIN = "HARD_DRAIN"
    CAP_ENFORCEMENT = "CAP_ENFORCEMENT"
    RECONCILE = "RECONCILE"


@dataclass(frozen=True)
class AllocatorConfig:
    """Allocator configuration.

    Default config:
      * evaluate every 30s
      * move 20% of remaining delta every 5 minutes (exponential smoothing)
      * allow 15% cap overshoot to account for miner granularity
    """

    poll_seconds: int = 30
    rebalance_rate: float = 0.20
    interval_seconds: int = 300
    overshoot_pct: float = 0.15
    miner_cooldown_seconds: int = 600
    max_switches_per_tick: int = 50

    # If enabled, orders below hashprice are treated as ineligible.
    enforce_hashprice_floor: bool = False

    # A small deadband to avoid micro-churn (in EH/s). If the delta is below
    # this value, the allocator will not try to correct it.
    rebalance_threshold_eh_s: float = 0.0

    # Labels considered "default" (non-market) destinations.
    default_labels: set[str] = field(default_factory=lambda: set(DEFAULT_LABELS))

    # --- Pool health monitoring ---
    # If enabled, the allocator can temporarily make an order ineligible when
    # the upstream pool appears unhealthy (no jobs, high unknown rejects, etc.).
    enable_pool_health: bool = True

    # Warmup window after an order starts receiving hashpower.
    pool_health_warmup_seconds: int = 120

    # Minimum share samples (over a tick) before using reject rates to trigger
    # state transitions.
    pool_health_min_samples: int = 20

    # If proxies report last_notify_age_s and it exceeds this threshold for all
    # miners on the order, treat the order as unhealthy.
    pool_health_no_notify_seconds: int = 120

    # Thresholds for reject-rate based health decisions.
    pool_health_other_reject_threshold: float = 0.20
    pool_health_reject_threshold: float = 0.50

    # EMA smoothing coefficient for health metrics (0..1).
    pool_health_ema_alpha: float = 0.30

    # Backoff policy after unhealthy detection.
    pool_health_backoff_base_seconds: int = 120
    pool_health_backoff_max_seconds: int = 900


@dataclass(frozen=True)
class OrderSnapshot:
    """Order data needed by the allocator."""

    order_id: str
    host: str
    port: int
    user: str
    password: str
    price_btc_eh_day: Decimal
    balance_btc: Decimal
    limit_eh_s: Optional[Decimal]
    queue_position_time: datetime
    status: str


@dataclass(frozen=True)
class MinerSnapshot:
    """Miner state as seen by the API from Redis heartbeats."""

    worker_ip: str
    hashrate_eh_s: float
    pool_label: str

    # Optional operational fields.
    accepted: int = 0
    rejected: int = 0
    rejected_stale: int = 0
    rejected_duplicate: int = 0
    rejected_low_diff: int = 0
    rejected_other: int = 0

    # Pool liveness signal: age in seconds since the last mining.notify observed
    # by the proxy for this miner group. None means unknown/not reported.
    last_notify_age_s: Optional[float] = None
    worker_name: str = ""


@dataclass(frozen=True)
class AllocationSnapshot:
    """A point-in-time snapshot that the allocator plans against."""

    now: datetime
    orders: list[OrderSnapshot]
    miners: list[MinerSnapshot]
    hashprice_btc_eh_day: Optional[Decimal] = None


@dataclass(frozen=True)
class PlanMove:
    """A single miner routing decision."""

    worker_ip: str
    from_label: str
    to_order_id: Optional[str]  # None means switch to default
    reason: MoveReason
    hashrate_eh_s: float


@dataclass(frozen=True)
class AllocationPlan:
    """A list of routing actions for the executor."""

    moves: list[PlanMove]


@dataclass(frozen=True)
class DecisionEvent:
    """A structured event that explains allocator decisions."""

    ts: datetime
    level: str
    code: str
    message: str
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OrderDiagnostics:
    order_id: str
    eligible: bool
    ineligible_reason: Optional[str]
    price_btc_eh_day: Decimal
    limit_eh_s: Optional[Decimal]
    balance_btc: Decimal
    target_eh_s: float
    current_eh_s: float
    budget_eh_s: float
    hard_drain: bool


@dataclass(frozen=True)
class TickDiagnostics:
    """A compact debug representation of what happened in a tick."""

    tick_ts: datetime
    dt_seconds: float
    supply_eh_s: float
    miner_count: int
    eligible_order_count: int
    hard_drain_order_count: int
    planned_switches: int
    alpha: float
    orders: list[OrderDiagnostics] = field(default_factory=list)
    events: list[DecisionEvent] = field(default_factory=list)
