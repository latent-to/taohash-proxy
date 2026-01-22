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
class RuntimeStateView:
    """Snapshot of allocator runtime state loaded from Redis.

    Loaded at the start of each tick via AllocatorRuntimeState.load_view() and
    passed to PlanningContext for use by the rules pipeline and planner.

    Provides:
        - last_tick_ts: Previous tick timestamp (for computing dt/alpha damping)
        - miner_last_move_at: Cooldown timestamps (prevents thrashing)
        - miner_counters: Cumulative share stats (for pool health evaluation)
        - order_health: Health status per order (HEALTHY/WARMUP/DEGRADED/BACKOFF)

    Frozen to ensure deterministic planning decisions within a single tick.
    """

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


# Redis-backed persistence implementation
class AllocatorRuntimeState:
    """Redis-backed persistence layer for allocator runtime state.

    Manages state that persists across allocator ticks and service restarts,
    ensuring consistent behavior even if the allocator process restarts.

    Used in:
        - Allocator loop: Loads state at start of each tick via load_view()
        - Executor: Persists miner moves and tick timestamps after execution

    Maintains four types of state in Redis:
        - Miner cooldowns: Timestamps of last miner moves (prevents thrashing
          by enforcing cooldown periods between moves)
        - Last tick timestamp: Previous tick time (for computing dt/alpha in
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
        worker_ips: list[str],
        order_ids: Optional[list[str]] = None,
    ) -> RuntimeStateView:
        """Load runtime state from Redis for the current allocator tick.

        Called at the start of each tick to retrieve persisted state needed for
        planning decisions. Returns an immutable RuntimeStateView that is passed
        to PlanningContext for use by the rules pipeline and planner.

        Args:
            worker_ips: List of miner worker IPs to load cooldown timestamps and
                    counters for. Typically all currently connected miners.
            order_ids: Optional list of order IDs to load health status for.
                    If None, only loads state for miners. Typically includes
                    active orders plus any orders currently being mined.

        Returns:
            RuntimeStateView: Immutable snapshot containing:
                - last_tick_ts: Previous tick timestamp (for dt/alpha damping)
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
            miner_last_move_at = await self._load_miner_last_move_at(worker_ips)
            miner_counters = await self._load_miner_counters(worker_ips)
            order_health = await self._load_order_health(order_ids)

            return RuntimeStateView(
                last_tick_ts=last_tick_ts,
                miner_last_move_at=miner_last_move_at,
                miner_counters=miner_counters,
                order_health=order_health,
            )
        except Exception as e:
            logger.error(f"Failed to load allocator runtime state: {e}")
            return self._empty_view()

    def _empty_view(self) -> RuntimeStateView:
        return RuntimeStateView(
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
        self, worker_ips: list[str]
    ) -> dict[str, datetime]:
        """Load miner cooldown timestamps from Redis.

        Retrieves the last move timestamp for each worker_ip from Redis keys
        (market:allocator:state:miner_last_move:{worker_ip}). Used to enforce
        cooldown periods and prevent thrashing by moving the same miner too
        frequently.

        Args:
            worker_ips: List of miner worker IPs to load timestamps for

        Returns:
            Dictionary mapping worker_ip -> last move datetime. Only includes
            entries that were successfully loaded (skips missing/invalid data).
        """
        miner_last_move_at: dict[str, datetime] = {}
        if not worker_ips:
            return miner_last_move_at

        keys = [self._miner_last_move_key(w) for w in worker_ips]
        raw_values = await self._redis.mget(keys)

        for worker_ip, raw in zip(worker_ips, raw_values):
            if raw is None:
                continue
            try:
                miner_last_move_at[worker_ip] = _from_unix_seconds(float(raw))
            except (TypeError, ValueError):
                continue

        return miner_last_move_at

    async def _load_miner_counters(
        self, worker_ips: list[str]
    ) -> dict[str, MinerCounterRecord]:
        """Load cumulative miner share statistics from Redis.

        Retrieves persistent counter records (accepted/rejected share counts) for
        each miner from Redis hash. These cumulative counters are used by pool
        health evaluation to compute deltas between ticks and detect pool issues.

        Args:
            worker_ips: List of miner worker IPs to load counters for

        Returns:
            Dictionary mapping worker_ip -> MinerCounterRecord.
        """
        miner_counters: dict[str, MinerCounterRecord] = {}
        if not worker_ips:
            return miner_counters

        raw_values = await self._redis.hmget(_HASH_MINER_COUNTERS, worker_ips)
        for worker_ip, raw in zip(worker_ips, raw_values):
            if not raw:
                continue
            try:
                payload = json.loads(raw)
                miner_counters[worker_ip] = MinerCounterRecord.from_dict(payload)
            except Exception:
                continue

        return miner_counters

    async def _load_order_health(
        self, order_ids: Optional[list[str]]
    ) -> dict[str, OrderHealthRecord]:
        """Load order health status records from Redis.

        Retrieves persistent health state for each order from Redis hash. Contains
        health status (HEALTHY/WARMUP/DEGRADED/BACKOFF), backoff timestamps, EMA
        metrics, and warmup windows. Used by rules pipeline to determine which
        orders can receive hashrate and which must be hard-drained.

        Args:
            order_ids: List of order IDs to load health records for. If None or
                    empty, returns empty dict.

        Returns:
            Dictionary mapping order_id -> OrderHealthRecord.
        """
        order_health: dict[str, OrderHealthRecord] = {}
        if not order_ids:
            return order_health

        raw_values = await self._redis.hmget(_HASH_ORDER_HEALTH, order_ids)
        for oid, raw in zip(order_ids, raw_values):
            if not raw:
                continue
            try:
                payload = json.loads(raw)
                order_health[oid] = OrderHealthRecord.from_dict(payload)
            except Exception:
                continue

        return order_health

    async def persist_miner_counters(
        self, counters: dict[str, MinerCounterRecord]
    ) -> None:
        """Persist miner counter records to Redis.

        Writes cumulative share statistics (accepted/rejected counts) to Redis hash
        for use in future ticks. Called after pool health evaluation to save updated
        counters that will be used to compute deltas in the next tick.

        Args:
            counters: Dictionary mapping worker_ip -> MinerCounterRecord to persist
        """
        if not self._redis or not counters:
            return
        try:
            mapping = {k: json.dumps(v.as_dict()) for k, v in counters.items()}
            await self._redis.hset(_HASH_MINER_COUNTERS, mapping=mapping)
        except Exception as e:
            logger.error(f"Failed to persist miner counters: {e}")

    async def persist_order_health(self, health: dict[str, OrderHealthRecord]) -> None:
        """Persist order health records to Redis.

        Writes health status (HEALTHY/WARMUP/DEGRADED/BACKOFF) with backoff timestamps
        and EMA metrics to Redis hash. Called after pool health evaluation to save
        updated health state that determines order eligibility in future ticks.

        Args:
            health: Dictionary mapping order_id -> OrderHealthRecord to persist
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
        (alpha = 1 - (1 - rate)^(dt/interval)).

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

    async def record_miner_moves(self, worker_ips: list[str], now: datetime) -> None:
        """Persist cooldown timestamps for miners that were moved.

        Records the current timestamp for each moved miner in Redis with TTL.
        These timestamps are used in future ticks to enforce cooldown periods
        (prevent moving the same miner too frequently).

        Args:
            worker_ips: List of miner worker IPs that were moved this tick
            now: Timestamp to record as the move time
        """
        if not self._redis or not worker_ips:
            return

        timestamp = str(_to_unix_seconds(now))
        ttl = self._miner_last_move_ttl_seconds
        try:
            pipe = self._redis.pipeline()
            for worker_ip in worker_ips:
                pipe.set(self._miner_last_move_key(worker_ip), timestamp, ex=ttl)
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

    def _miner_last_move_key(self, worker_ip: str) -> str:
        return f"{_KEY_MINER_LAST_MOVE_PREFIX}{worker_ip}"
