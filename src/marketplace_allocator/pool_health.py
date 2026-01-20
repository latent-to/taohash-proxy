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

