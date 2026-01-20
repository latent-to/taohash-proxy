from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from src.marketplace_allocator.types import AllocatorConfig, MinerSnapshot


class OrderHealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    WARMUP = "WARMUP"
    DEGRADED = "DEGRADED"
    BACKOFF = "BACKOFF"


def _utc_now_ts(now: datetime) -> float:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return float(now.timestamp())


@dataclass
class MinerCounterRecord:
    """Persistent per-miner counters used to compute deltas between ticks."""

    accepted: int = 0
    rejected: int = 0
    rejected_stale: int = 0
    rejected_duplicate: int = 0
    rejected_low_diff: int = 0
    rejected_other: int = 0
    pool_label: str = "unknown"

    @classmethod
    def from_snapshot(cls, m: MinerSnapshot) -> "MinerCounterRecord":
        return cls(
            accepted=int(m.accepted or 0),
            rejected=int(m.rejected or 0),
            rejected_stale=int(m.rejected_stale or 0),
            rejected_duplicate=int(m.rejected_duplicate or 0),
            rejected_low_diff=int(m.rejected_low_diff or 0),
            rejected_other=int(m.rejected_other or 0),
            pool_label=str(m.pool_label or "unknown"),
        )

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MinerCounterRecord":
        return cls(
            accepted=int(data.get("accepted") or 0),
            rejected=int(data.get("rejected") or 0),
            rejected_stale=int(data.get("rejected_stale") or 0),
            rejected_duplicate=int(data.get("rejected_duplicate") or 0),
            rejected_low_diff=int(data.get("rejected_low_diff") or 0),
            rejected_other=int(data.get("rejected_other") or 0),
            pool_label=str(data.get("pool_label") or "unknown"),
        )


@dataclass
class OrderHealthRecord:
    """Persistent per-order health state."""

    status: OrderHealthStatus = OrderHealthStatus.HEALTHY
    reason: str = ""

    # Unix timestamps (seconds). 0 means unset.
    warmup_until_ts: float = 0.0 # When warmup ends
    backoff_until_ts: float = 0.0 # When backoff expires

    # Backoff exponent for repeated failures.
    backoff_level: int = 0 # 0-10, 10 is max

    # Smoothed metrics (EMA)
    ema_reject_rate: float = 0.0
    ema_other_reject_rate: float = 0.0
    ema_job_gap_s: float = 0.0

    # Unix timestamp of last evaluation.
    last_update_ts: float = 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "OrderHealthRecord":
        status_raw = str(data.get("status") or OrderHealthStatus.HEALTHY.value)
        try:
            status = OrderHealthStatus(status_raw)
        except Exception:
            status = OrderHealthStatus.HEALTHY
        return cls(
            status=status,
            reason=str(data.get("reason") or ""),
            warmup_until_ts=float(data.get("warmup_until_ts") or 0.0),
            backoff_until_ts=float(data.get("backoff_until_ts") or 0.0),
            backoff_level=int(data.get("backoff_level") or 0),
            ema_reject_rate=float(data.get("ema_reject_rate") or 0.0),
            ema_other_reject_rate=float(data.get("ema_other_reject_rate") or 0.0),
            ema_job_gap_s=float(data.get("ema_job_gap_s") or 0.0),
            last_update_ts=float(data.get("last_update_ts") or 0.0),
        )


@dataclass
class OrderHealthTick:
    """One-tick aggregated deltas for an order."""

    delta_accepted: int = 0
    delta_rejected: int = 0
    delta_other_rejected: int = 0
    delta_total: int = 0

    # Smallest last_notify_age_s across miners on the order.
    # None means not available.
    min_last_notify_age_s: Optional[float] = None

    def observe_notify_age(self, age: Optional[float]) -> None:
        if age is None:
            return
        if self.min_last_notify_age_s is None:
            self.min_last_notify_age_s = float(age)
        else:
            self.min_last_notify_age_s = min(self.min_last_notify_age_s, float(age))


@dataclass
class PoolHealthDecision:
    order_health: dict[str, OrderHealthRecord]
    miner_counters: dict[str, MinerCounterRecord]
    events: list[dict]

