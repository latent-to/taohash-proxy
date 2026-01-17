"""Allocator runtime state.

The allocator stores its state across ticks:
  * per-miner cooldown (don't move the same miner too frequently)
  * last-tick timestamp (for dt/alpha computations)

Storing this in Redis keeps behavior stable across restarts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import json
import os

from src.marketplace_allocator.pool_health import MinerCounterRecord, OrderHealthRecord
from src.utils.logger import get_logger
from src.utils.redis_client import RedisClient

logger = get_logger(__name__)


_PREFIX = "market:allocator:state"
_HASH_META = f"{_PREFIX}:meta"
_HASH_MINER_COUNTERS = f"{_PREFIX}:miner_counters"
_HASH_ORDER_HEALTH = f"{_PREFIX}:order_health"


_KEY_MINER_LAST_MOVE_PREFIX = f"{_PREFIX}:miner_last_move:"
_MINER_LAST_MOVE_TTL_SECONDS_DEFAULT = int(
    os.getenv("ORDER_ALLOCATOR_MINER_LAST_MOVE_TTL_SECONDS", "3600")
)

_META_LAST_TICK_TS = "last_tick_ts"


def _to_unix_seconds(ts: datetime) -> float:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.timestamp()


def _from_unix_seconds(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


@dataclass(frozen=True)
class RuntimeStateView:
    """View of allocator runtime state for a single tick."""

    last_tick_ts: Optional[datetime]
    miner_last_move_at: dict[str, datetime]
    miner_counters: dict[str, "MinerCounterRecord"]
    order_health: dict[str, "OrderHealthRecord"]

    def miner_in_cooldown(self, worker_ip: str, now: datetime, cooldown_s: int) -> bool:
        last = self.miner_last_move_at.get(worker_ip)
        if not last:
            return False
        elapsed = (now - last).total_seconds()
        return elapsed < cooldown_s


class AllocatorRuntimeState:
    """Redis-backed persistence for allocator state across ticks.

    The allocator maintains state of different things between runs:
    - When each miner was last moved (cooldown enforcement)
    - When the last tick ran (for computing dt in the damping formula)
    - Per-miner share counters (for pool health tracking)
    - Per-order health status (HEALTHY/WARMUP/DEGRADED/BACKOFF)
    """

    def __init__(self, redis_client: RedisClient):
        self._redis_client = redis_client

    @property
    def _redis(self):
        return getattr(self._redis_client, "redis", None)
