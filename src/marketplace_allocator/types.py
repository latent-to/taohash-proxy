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

    # A small threshold to avoid micro-churn (in EH/s). If the delta is below
    # this value, the allocator will not try to correct it.
    # TODO: Make the threshold dynamic based on the required hashrate
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


@dataclass
class MarketplaceOrder:
    """
    Used as the primary typed representation of orders throughout.
    Can be converted to OrderSnapshot for allocator use or to dict for API responses.
    """

    order_id: str
    host: str
    port: int
    user: str
    password: str
    price_btc_eh_day: Decimal
    amount_btc: Decimal
    balance_btc: Decimal
    limit_eh_s: Optional[Decimal]
    status: str
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    queue_position_time: Optional[datetime]
    fulfilled_at: Optional[datetime]
    paused_at: Optional[datetime]
    cancelled_at: Optional[datetime]
    last_rebalance_at: Optional[datetime]
    total_shares_credited: int
    total_share_value_credited: Decimal
    last_share_credited_at: Optional[datetime]

    @classmethod
    def from_db_row(cls, row) -> "MarketplaceOrder":
        """Create from database row."""
        return cls(
            order_id=row["order_id"],
            host=row["host"],
            port=int(row["port"]),
            user=row["username"],
            password=row["password"],
            price_btc_eh_day=row["price_btc_eh_day"],
            amount_btc=row["amount_btc"],
            balance_btc=row["balance_btc"],
            limit_eh_s=row["limit_eh_s"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            queue_position_time=row["queue_position_time"],
            fulfilled_at=row["fulfilled_at"],
            paused_at=row["paused_at"],
            cancelled_at=row["cancelled_at"],
            last_rebalance_at=row["last_rebalance_at"],
            total_shares_credited=row["total_shares_credited"] or 0,
            total_share_value_credited=row["total_share_value_credited"] or Decimal(0),
            last_share_credited_at=row["last_share_credited_at"],
        )

    def to_snapshot(self) -> OrderSnapshot:
        """Convert to allocator snapshot."""
        return OrderSnapshot(
            order_id=self.order_id,
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            price_btc_eh_day=self.price_btc_eh_day,
            balance_btc=self.balance_btc,
            limit_eh_s=self.limit_eh_s,
            queue_position_time=self.queue_position_time,
            status=self.status,
        )

    def to_api_dict(self) -> dict:
        """Convert to API response format (Decimals as strings, ISO dates)."""
        return {
            "order_id": self.order_id,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "status": self.status,
            "price_btc_eh_day": str(self.price_btc_eh_day),
            "amount_btc": str(self.amount_btc),
            "balance_btc": str(self.balance_btc),
            "limit_eh_s": str(self.limit_eh_s) if self.limit_eh_s else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "queue_position_time": (
                self.queue_position_time.isoformat()
                if self.queue_position_time
                else None
            ),
            "fulfilled_at": (
                self.fulfilled_at.isoformat() if self.fulfilled_at else None
            ),
            "paused_at": self.paused_at.isoformat() if self.paused_at else None,
            "cancelled_at": (
                self.cancelled_at.isoformat() if self.cancelled_at else None
            ),
            "last_rebalance_at": (
                self.last_rebalance_at.isoformat() if self.last_rebalance_at else None
            ),
            "total_shares_credited": self.total_shares_credited,
            "total_share_value_credited": str(self.total_share_value_credited),
            "last_share_credited_at": (
                self.last_share_credited_at.isoformat()
                if self.last_share_credited_at
                else None
            ),
        }


@dataclass(frozen=True)
class MinerSnapshot:
    """Miner state as seen by the API from Redis heartbeats."""

    miner_id: str
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

    @classmethod
    def from_redis_stats(cls, miner_id: str, stats: dict) -> "MinerSnapshot":
        """Create from Redis aggregated stats dict."""
        hashrate_gh = float(stats.get("hashrate_gh") or 0.0)
        rejected_breakdown = stats.get("rejected_breakdown") or {}
        return cls(
            miner_id=miner_id,
            hashrate_eh_s=gh_to_eh(hashrate_gh),
            pool_label=str(stats.get("pool_label") or "unknown"),
            accepted=int(stats.get("accepted") or 0),
            rejected=int(stats.get("rejected") or 0),
            rejected_stale=int(rejected_breakdown.get("stale") or 0),
            rejected_duplicate=int(rejected_breakdown.get("duplicate") or 0),
            rejected_low_diff=int(rejected_breakdown.get("low_diff") or 0),
            rejected_other=int(rejected_breakdown.get("other") or 0),
            last_notify_age_s=(
                float(stats.get("last_notify_age_s"))
                if stats.get("last_notify_age_s") is not None
                else None
            ),
            worker_name=str(stats.get("worker_name") or ""),
        )


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

    miner_id: str  # Format: "worker_name:ip_address"
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
    damping_factor: float
    orders: list[OrderDiagnostics] = field(default_factory=list)
    events: list[DecisionEvent] = field(default_factory=list)
