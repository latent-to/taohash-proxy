from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from typing import Any, Optional

from tests.framework.chaos import FaultPlan
from tests.framework.clock import DeterministicClock


@dataclass(frozen=True)
class PublishedMessage:
    """A message published via Redis pub/sub."""

    channel: str
    message: str
    ts: float


class InMemoryRedisPipeline:
    """Subset of redis-py's pipeline API used by the allocator."""

    def __init__(self, redis: "InMemoryRedis"):
        self._redis = redis
        self._ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def set(self, key: str, value: str, ex: Optional[int] = None) -> "InMemoryRedisPipeline":
        self._ops.append(("set", (key, value), {"ex": ex}))
        return self

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for name, args, kwargs in self._ops:
            results.append(await getattr(self._redis, name)(*args, **kwargs))
        self._ops.clear()
        return results


class InMemoryRedis:
    """An in-memory subset of redis.asyncio.Redis.

    Only implements methods required by the allocator tests:
      - get/set/delete/keys
      - hget/hset/hmget
      - mget
      - publish
      - pipeline(set...)/execute

    TTL support is implemented for string keys created via `set(..., ex=ttl)`.

    The mock integrates with DeterministicClock and FaultPlan.
    """

    def __init__(
        self,
        *,
        clock: DeterministicClock,
        faults: Optional[FaultPlan] = None,
    ) -> None:
        self._clock = clock
        self._faults = faults or FaultPlan()

        self._strings: dict[str, str] = {}
        self._expiry_ts: dict[str, float] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._published: list[PublishedMessage] = []

    def _purge_if_expired(self, key: str) -> None:
        exp = self._expiry_ts.get(key)
        if exp is None:
            return
        if self._clock.unix() < float(exp):
            return
        self._expiry_ts.pop(key, None)
        self._strings.pop(key, None)

    def _all_keys(self) -> set[str]:
        return set(self._strings.keys()) | set(self._hashes.keys())

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def get(self, key: str) -> Optional[str]:
        await self._faults.before("redis.get", self._clock)
        key = str(key)
        self._purge_if_expired(key)
        return self._strings.get(key)

    async def set(self, key: str, value: str, ex: Optional[int] = None) -> bool:
        await self._faults.before("redis.set", self._clock)
        key = str(key)
        self._strings[key] = str(value)
        if ex is not None:
            self._expiry_ts[key] = self._clock.unix() + float(ex)
        else:
            self._expiry_ts.pop(key, None)
        return True

    async def delete(self, *keys: str) -> int:
        await self._faults.before("redis.delete", self._clock)
        deleted = 0
        for key in keys:
            key = str(key)
            self._purge_if_expired(key)
            if key in self._strings:
                deleted += 1
                self._strings.pop(key, None)
                self._expiry_ts.pop(key, None)
            if key in self._hashes:
                deleted += 1
                self._hashes.pop(key, None)
        return deleted

    async def keys(self, pattern: str) -> list[str]:
        await self._faults.before("redis.keys", self._clock)
        pattern = str(pattern)
        result: list[str] = []
        for key in sorted(self._all_keys()):
            self._purge_if_expired(key)
            if fnmatch.fnmatch(key, pattern):
                result.append(key)
        return result

    async def mget(self, keys: list[str]) -> list[Optional[str]]:
        await self._faults.before("redis.mget", self._clock)
        out: list[Optional[str]] = []
        for key in keys:
            out.append(await self.get(key))
        return out

    async def publish(self, channel: str, message: str) -> int:
        await self._faults.before("redis.publish", self._clock)
        self._published.append(
            PublishedMessage(channel=str(channel), message=str(message), ts=self._clock.unix())
        )
        return 0

    def pipeline(self) -> InMemoryRedisPipeline:
        return InMemoryRedisPipeline(self)

    async def hget(self, name: str, key: str) -> Optional[str]:
        await self._faults.before("redis.hget", self._clock)
        return (self._hashes.get(str(name)) or {}).get(str(key))

    async def hset(
        self,
        name: str,
        key: Optional[str] = None,
        value: Optional[str] = None,
        *,
        mapping: Optional[dict[str, str]] = None,
    ) -> int:
        await self._faults.before("redis.hset", self._clock)
        name = str(name)
        h = self._hashes.setdefault(name, {})

        changed = 0
        if mapping is not None:
            for k, v in mapping.items():
                k = str(k)
                v = str(v)
                if h.get(k) != v:
                    changed += 1
                h[k] = v
            return changed

        if key is None:
            raise TypeError("hset requires `key` when `mapping` is not provided")

        k = str(key)
        v = "" if value is None else str(value)
        if h.get(k) != v:
            changed += 1
        h[k] = v
        return changed

    async def hmget(self, name: str, keys: list[str]) -> list[Optional[str]]:
        await self._faults.before("redis.hmget", self._clock)
        name = str(name)
        h = self._hashes.get(name) or {}
        return [h.get(str(k)) for k in keys]

    async def hgetall(self, name: str) -> dict[str, str]:
        await self._faults.before("redis.hgetall", self._clock)
        return dict(self._hashes.get(str(name)) or {})

    def published(self, channel: Optional[str] = None) -> list[PublishedMessage]:
        if channel is None:
            return list(self._published)
        return [m for m in self._published if m.channel == channel]

    def published_json(self, channel: Optional[str] = None) -> list[dict[str, Any]]:
        """Parse published messages as JSON payloads."""
        out: list[dict[str, Any]] = []
        for msg in self.published(channel=channel):
            try:
                out.append(json.loads(msg.message))
            except Exception:
                continue
        return out


class RedisClientSpy:
    """A minimal RedisClient replacement for tests.

    Production code uses `src.utils.redis_client.RedisClient`, but the allocator
    runtime state store only needs an object with a `.redis` attribute.

    We add a couple of helper APIs used in end-to-end scenario tests.
    """

    def __init__(self, redis: InMemoryRedis):
        self.redis = redis

    async def get_hashprice_cache(self) -> Optional[dict[str, Any]]:
        raw = await self.redis.get("market:hashprice")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def set_hashprice_cache(self, payload: dict[str, Any], ttl_seconds: int = 1800) -> None:
        await self.redis.set("market:hashprice", json.dumps(payload), ex=int(ttl_seconds))


class RedisPublisherSpy:
    """A minimal RedisPublisher that publishes deterministic command payloads."""

    def __init__(self, redis: InMemoryRedis, *, channel: str = "miner:commands") -> None:
        self.redis = redis
        self.channel = channel

    async def publish_switch_pool(
        self,
        miner_id: str,
        pool_config: dict[str, Any],
        session_id: str | None = None,
    ) -> bool:
        payload = {
            "type": "switch_pool",
            "miner_id": str(miner_id),
            **({"session_id": session_id} if session_id else {}),
            "pool_config": dict(pool_config),
        }
        await self.redis.publish(self.channel, json.dumps(payload))
        return True

    async def publish_switch_default(self, miner_id: str, session_id: str | None = None) -> bool:
        payload = {
            "type": "switch_default",
            "miner_id": str(miner_id),
            **({"session_id": session_id} if session_id else {}),
        }
        await self.redis.publish(self.channel, json.dumps(payload))
        return True

    def published_commands(self) -> list[dict[str, Any]]:
        return self.redis.published_json(channel=self.channel)
