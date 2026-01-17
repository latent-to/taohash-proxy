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

    async def load_view(
        self,
        worker_ips: list[str],
        order_ids: Optional[list[str]] = None,
    ) -> RuntimeStateView:
        """
        Load runtime state for this tick.

        This view contains:
          * last_tick timestamp (for dt/alpha)
          * per-miner last move timestamps (cooldowns)
          * per-miner cumulative counters (for pool health)
          * per-order pool health state (HEALTHY/WARMUP/DEGRADED/BACKOFF)

        Args:
            worker_ips: List of worker IPs to load state for.
            order_ids: List of order IDs to load state for.

        Returns:
            RuntimeStateView: View of allocator runtime state for this tick.
        """

        if not self._redis:
            return RuntimeStateView(
                last_tick_ts=None,
                miner_last_move_at={},
                miner_counters={},
                order_health={},
            )

        try:
            last_tick_raw = await self._redis.hget(_HASH_META, _META_LAST_TICK_TS)
            last_tick_ts = (
                _from_unix_seconds(float(last_tick_raw)) if last_tick_raw else None
            )

            # Fetch last movement information from miners
            miner_last_move_at: dict[str, datetime] = {}
            if worker_ips:
                keys = [self._miner_last_move_key(w) for w in worker_ips]
                raw_values = await self._redis.mget(keys)
                for worker_ip, raw in zip(worker_ips, raw_values):
                    if raw is None:
                        continue
                    try:
                        miner_last_move_at[worker_ip] = _from_unix_seconds(float(raw))
                    except (TypeError, ValueError):
                        continue

            # Fetch per-miner share share stats for pool health tracking
            miner_counters: dict[str, "MinerCounterRecord"] = {}
            if worker_ips:
                raw_values = await self._redis.hmget(_HASH_MINER_COUNTERS, worker_ips)
                for worker_ip, raw in zip(worker_ips, raw_values):
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                        miner_counters[worker_ip] = MinerCounterRecord.from_dict(
                            payload
                        )
                    except Exception:
                        continue

            # Fetch per-order health status
            order_health: dict[str, "OrderHealthRecord"] = {}
            if order_ids:
                raw_values = await self._redis.hmget(_HASH_ORDER_HEALTH, order_ids)
                for oid, raw in zip(order_ids, raw_values):
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                        order_health[oid] = OrderHealthRecord.from_dict(payload)
                    except Exception:
                        continue

            return RuntimeStateView(
                last_tick_ts=last_tick_ts,
                miner_last_move_at=miner_last_move_at,
                miner_counters=miner_counters,
                order_health=order_health,
            )
        except Exception as e:
            logger.error("Failed to load allocator runtime state: %s", e)
            return RuntimeStateView(
                last_tick_ts=None,
                miner_last_move_at={},
                miner_counters={},
                order_health={},
            )
