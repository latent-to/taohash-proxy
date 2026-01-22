"""Allocator runtime state.

The allocator stores its state across ticks:
  * per-miner cooldown (don't move the same miner too frequently)
  * last-tick timestamp (for dt/damping_factor computations)

Storing this in Redis keeps behavior stable across restarts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import json
import os

from src.marketplace_allocator.pool_health import MinerPoolShareStats, PoolHealthRecord
from src.utils.logger import get_logger
from src.utils.redis_client import RedisClient

logger = get_logger(__name__)

# Redis keys / schema
_PREFIX = "market:allocator:state"
_HASH_META = f"{_PREFIX}:meta"
_HASH_MINER_COUNTERS = f"{_PREFIX}:miner_counters"
_HASH_ORDER_HEALTH = f"{_PREFIX}:order_health"

_KEY_MINER_LAST_MOVE_PREFIX = f"{_PREFIX}:miner_last_move:"
_META_LAST_TICK_TS = "last_tick_ts"

_MINER_LAST_MOVE_TTL_SECONDS_DEFAULT = int(
    os.getenv("ORDER_ALLOCATOR_MINER_LAST_MOVE_TTL_SECONDS", "3600")
)


# Time helpers
def _to_unix_seconds(ts: datetime) -> float:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.timestamp()


def _from_unix_seconds(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


# Runtime view used by planner/rules
@dataclass(frozen=True)
class RuntimeStateSnapshot:
    """Snapshot of allocator runtime state loaded from Redis.

    Loaded at the start of each tick via RuntimeStateStore.load_view() and
    passed to PlanningInput for use by the rules pipeline and planner.

    Provides:
        - last_tick_ts: Previous tick timestamp (for computing dt/damping_factor damping)
        - miner_last_move_at: Cooldown timestamps (prevents thrashing)
        - miner_counters: Cumulative share stats (for pool health evaluation)
        - order_health: Health status per order (HEALTHY/WARMUP/DEGRADED/BACKOFF)

    Frozen to ensure deterministic planning decisions within a single tick.
    """

    last_tick_ts: Optional[datetime]
    miner_last_move_at: dict[str, datetime]
    miner_counters: dict[str, "MinerPoolShareStats"]
    order_health: dict[str, "PoolHealthRecord"]

    def miner_in_cooldown(self, miner_id: str, now: datetime, cooldown_s: int) -> bool:
        last = self.miner_last_move_at.get(miner_id)
        if not last:
            return False
        elapsed = (now - last).total_seconds()
        return elapsed < cooldown_s


# Redis-backed persistence implementation
class RuntimeStateStore:
    """Redis-backed persistence layer for allocator runtime state.

    Manages state that persists across allocator ticks and service restarts,
    ensuring consistent behavior even if the allocator process restarts.

    Used in:
        - Allocator loop: Loads state at start of each tick via load_view()
        - Executor: Persists miner moves and tick timestamps after execution

    Maintains four types of state in Redis:
        - Miner cooldowns: Timestamps of last miner moves (prevents thrashing
          by enforcing cooldown periods between moves)
        - Last tick timestamp: Previous tick time (for computing dt/damping_factor in
          exponential damping formula)
        - Miner counters: Cumulative share statistics per miner (accepted/rejected
          counts used for pool health evaluation)
        - Order health: Per-order health status (HEALTHY/WARMUP/DEGRADED/BACKOFF)
          determines which orders can receive hashrate

    Redis storage ensures state survives restarts, maintaining cooldown periods
    and pool health continuity across service deployments.
    """

    def __init__(self, redis_client: RedisClient):
        self._redis_client = redis_client

    @property
    def _redis(self):
        return getattr(self._redis_client, "redis", None)

    async def load_view(
        self,
        miner_ids: list[str],
        order_ids: Optional[list[str]] = None,
    ) -> RuntimeStateSnapshot:
        """Load runtime state from Redis for the current allocator tick.

        Called at the start of each tick to retrieve persisted state needed for
        planning decisions. Returns an immutable RuntimeStateSnapshot that is passed
        to PlanningInput for use by the rules pipeline and planner.

        Args:
            miner_ids: List of miner worker IPs to load cooldown timestamps and
                    counters for. Typically all currently connected miners.
            order_ids: Optional list of order IDs to load health status for.
                    If None, only loads state for miners. Typically includes
                    active orders plus any orders currently being mined.

        Returns:
            RuntimeStateSnapshot: Immutable snapshot containing:
                - last_tick_ts: Previous tick timestamp (for dt/damping_factor damping)
                - miner_last_move_at: Cooldown timestamps per miner
                - miner_counters: Cumulative share stats per miner
                - order_health: Health status per order

        Note:
            Returns empty view (all fields empty/None) if Redis is unavailable.
        """
        if not self._redis:
            return self._empty_view()

        try:
            last_tick_ts = await self._load_last_tick_ts()
            miner_last_move_at = await self._load_miner_last_move_at(miner_ids)
            miner_counters = await self._load_miner_share_stats(miner_ids)
            order_health = await self._load_order_health(order_ids)

            return RuntimeStateSnapshot(
                last_tick_ts=last_tick_ts,
                miner_last_move_at=miner_last_move_at,
                miner_counters=miner_counters,
                order_health=order_health,
            )
        except Exception as e:
            logger.error(f"Failed to load allocator runtime state: {e}")
            return self._empty_view()

    def _empty_view(self) -> RuntimeStateSnapshot:
        return RuntimeStateSnapshot(
            last_tick_ts=None,
            miner_last_move_at={},
            miner_counters={},
            order_health={},
        )

    async def _load_last_tick_ts(self) -> Optional[datetime]:
        """Load last tick timestamp from Redis.

        Retrieves the timestamp of the previous allocator tick from Redis meta hash.
        Used to compute dt (time delta) for the exponential damping formula in the
        planner. Returns None if no previous tick exists (first run).

        Returns:
            Previous tick timestamp, or None if no previous tick recorded.
        """
        last_tick_raw = await self._redis.hget(_HASH_META, _META_LAST_TICK_TS)
        return _from_unix_seconds(float(last_tick_raw)) if last_tick_raw else None

    async def _load_miner_last_move_at(
        self, miner_ids: list[str]
    ) -> dict[str, datetime]:
        """Load miner cooldown timestamps from Redis.

        Retrieves the last move timestamp for each miner_id from Redis keys
        (market:allocator:state:miner_last_move:{miner_id}). Used to enforce
        cooldown periods and prevent thrashing by moving the same miner too
        frequently.

        Args:
            miner_ids: List of miner worker IPs to load timestamps for

        Returns:
            Dictionary mapping miner_id -> last move datetime. Only includes
            entries that were successfully loaded (skips missing/invalid data).
        """
        miner_last_move_at: dict[str, datetime] = {}
        if not miner_ids:
            return miner_last_move_at

        keys = [self._miner_last_move_key(w) for w in miner_ids]
        raw_values = await self._redis.mget(keys)

        for miner_id, raw in zip(miner_ids, raw_values):
            if raw is None:
                continue
            try:
                miner_last_move_at[miner_id] = _from_unix_seconds(float(raw))
            except (TypeError, ValueError):
                continue

        return miner_last_move_at

    async def _load_miner_share_stats(
        self, miner_ids: list[str]
    ) -> dict[str, MinerPoolShareStats]:
        """Load cumulative miner share statistics from Redis.

        Retrieves persistent counter records (accepted/rejected share counts) for
        each miner from Redis hash. These cumulative counters are used by pool
        health evaluation to compute deltas between ticks and detect pool issues.

        Args:
            miner_ids: List of miner worker IPs to load counters for

        Returns:
            Dictionary mapping miner_id -> MinerPoolShareStats.
        """
        miner_counters: dict[str, MinerPoolShareStats] = {}
        if not miner_ids:
            return miner_counters

        raw_values = await self._redis.hmget(_HASH_MINER_COUNTERS, miner_ids)
        for miner_id, raw in zip(miner_ids, raw_values):
            if not raw:
                continue
            try:
                payload = json.loads(raw)
                miner_counters[miner_id] = MinerPoolShareStats.from_dict(payload)
            except Exception:
                continue

        return miner_counters

    async def _load_order_health(
        self, order_ids: Optional[list[str]]
    ) -> dict[str, PoolHealthRecord]:
        """Load order health status records from Redis.

        Retrieves persistent health state for each order from Redis hash. Contains
        health status (HEALTHY/WARMUP/DEGRADED/BACKOFF), backoff timestamps, EMA
        metrics, and warmup windows. Used by rules pipeline to determine which
        orders can receive hashrate and which must be hard-drained.

        Args:
            order_ids: List of order IDs to load health records for. If None or
                    empty, returns empty dict.

        Returns:
            Dictionary mapping order_id -> PoolHealthRecord.
        """
        order_health: dict[str, PoolHealthRecord] = {}
        if not order_ids:
            return order_health

        raw_values = await self._redis.hmget(_HASH_ORDER_HEALTH, order_ids)
        for oid, raw in zip(order_ids, raw_values):
            if not raw:
                continue
            try:
                payload = json.loads(raw)
                order_health[oid] = PoolHealthRecord.from_dict(payload)
            except Exception:
                continue

        return order_health

    async def persist_miner_counters(
        self, counters: dict[str, MinerPoolShareStats]
    ) -> None:
        """Persist miner counter records to Redis.

        Writes cumulative share statistics (accepted/rejected counts) to Redis hash
        for use in future ticks. Called after pool health evaluation to save updated
        counters that will be used to compute deltas in the next tick.

        Args:
            counters: Dictionary mapping miner_id -> MinerPoolShareStats to persist
        """
        if not self._redis or not counters:
            return
        try:
            mapping = {k: json.dumps(v.as_dict()) for k, v in counters.items()}
            await self._redis.hset(_HASH_MINER_COUNTERS, mapping=mapping)
        except Exception as e:
            logger.error(f"Failed to persist miner counters: {e}")

    async def persist_order_health(self, health: dict[str, PoolHealthRecord]) -> None:
        """Persist order health records to Redis.

        Writes health status (HEALTHY/WARMUP/DEGRADED/BACKOFF) with backoff timestamps
        and EMA metrics to Redis hash. Called after pool health evaluation to save
        updated health state that determines order eligibility in future ticks.

        Args:
            health: Dictionary mapping order_id -> PoolHealthRecord to persist
        """
        if not self._redis or not health:
            return
        try:
            mapping = {k: json.dumps(v.as_dict()) for k, v in health.items()}
            await self._redis.hset(_HASH_ORDER_HEALTH, mapping=mapping)
        except Exception as e:
            logger.error(f"Failed to persist order health: {e}")

    async def set_last_tick(self, now: datetime) -> None:
        """Persist the current tick timestamp to Redis.

        Stores the timestamp of the current allocator tick in Redis. Used in the
        next tick to compute dt (time delta) for the exponential damping formula
        (damping_factor = 1 - (1 - rate)^(dt/interval)).

        Args:
            now: Current timestamp to store as last tick time
        """
        if not self._redis:
            return
        try:
            await self._redis.hset(
                _HASH_META, _META_LAST_TICK_TS, str(_to_unix_seconds(now))
            )
        except Exception as e:
            logger.error(f"Failed to persist allocator last_tick_ts: {e}")

    async def record_miner_moves(self, miner_ids: list[str], now: datetime) -> None:
        """Persist cooldown timestamps for miners that were moved.

        Records the current timestamp for each moved miner in Redis with TTL.
        These timestamps are used in future ticks to enforce cooldown periods
        (prevent moving the same miner too frequently).

        Args:
            miner_ids: List of miner worker IPs that were moved this tick
            now: Timestamp to record as the move time
        """
        if not self._redis or not miner_ids:
            return

        timestamp = str(_to_unix_seconds(now))
        ttl = self._miner_last_move_ttl_seconds
        try:
            pipe = self._redis.pipeline()
            for miner_id in miner_ids:
                pipe.set(self._miner_last_move_key(miner_id), timestamp, ex=ttl)
            await pipe.execute()
        except Exception as e:
            logger.error(f"Failed to persist miner cooldown timestamps: {e}")

    @property
    def _miner_last_move_ttl_seconds(self) -> int:
        """Compute TTL for miner last move keys in Redis.

        TTL ensures cooldown data persists long enough to enforce cooldown periods
        even if ticks are delayed. TTL is set to at least 2x the cooldown period
        to ensure data availability.

        Returns:
            TTL in seconds. Minimum 60s, or max of configured TTL default and
            2x the miner cooldown period.
        """
        try:
            cooldown_env = int(
                os.getenv("ORDER_ALLOCATOR_MINER_COOLDOWN_SECONDS", "600")
            )
        except Exception:
            cooldown_env = 600

        ttl = int(_MINER_LAST_MOVE_TTL_SECONDS_DEFAULT)
        return max(60, ttl, cooldown_env * 2)

    def _miner_last_move_key(self, miner_id: str) -> str:
        return f"{_KEY_MINER_LAST_MOVE_PREFIX}{miner_id}"
