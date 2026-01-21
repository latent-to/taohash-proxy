"""
Redis client utilities for pub/sub communication.

This module provides Redis publisher and subscriber functionality for
coordinating miner pool switches across multiple proxy instances.
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as redis
from redis.exceptions import ResponseError

from .logger import get_logger
from ..core.heartbeat_stats import MinerGroupStats

logger = get_logger(__name__)

REDIS_CHANNEL = "miner:commands"
ORDER_SHARE_STREAM = "order_shares"
ORDER_SHARE_GROUP = "order_share_processing"
ORDER_SHARE_CONSUMER = "share_processing_worker"
ORDER_SHARE_STREAM_MAXLEN = 10_000_000
HASHPRICE_CACHE_KEY = "market:hashprice"
HASHPRICE_CACHE_TTL_SECONDS = int(os.getenv("HASHPRICE_CACHE_TTL_SECONDS", "1800"))


class RedisPublisher:
    """
    Redis publisher for sending miner control commands.

    Used by the API service to broadcast pool switch commands to all proxy instances.
    """

    def __init__(self):
        self.redis: Optional[redis.Redis] = None
        self.channel = REDIS_CHANNEL

    async def connect(self) -> bool:
        """
        Connect to Redis server.

        Returns:
            True if connected successfully, False otherwise
        """
        redis_host = os.getenv("REDIS_HOST", "localhost")
        redis_port = int(os.getenv("REDIS_PORT", "6379"))

        try:
            self.redis = redis.Redis(
                host=redis_host,
                port=redis_port,
                decode_responses=True,
            )

            await self.redis.ping()
            logger.info(f"Connected to Redis at {redis_host}:{redis_port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            return False

    async def publish_switch_pool(
        self,
        worker_ip: str,
        pool_config: dict,
        session_id: str | None = None,
    ) -> bool:
        """
        Publish command to switch a miner to a specific pool.

        Args:
            worker_ip: Miner identifier (worker:ip format)
            pool_config: Pool configuration dict with host, port, user, pass, label

        Returns:
            True if published successfully, False otherwise
        """
        if not self.redis:
            logger.error("Redis not connected")
            return False

        try:
            message = json.dumps(
                {
                    "type": "switch_pool",
                    "worker_ip": worker_ip,
                    **({"session_id": session_id} if session_id else {}),
                    "pool_config": pool_config,
                }
            )
            await self.redis.publish(self.channel, message)
            logger.info(f"Published switch_pool command for {worker_ip}")
            return True
        except Exception as e:
            logger.error(f"Failed to publish switch_pool: {e}")
            return False

    async def publish_switch_default(
        self, worker_ip: str, session_id: str | None = None
    ) -> bool:
        """
        Publish command to switch a miner back to their default pool.

        The proxy determines the default pool from MinerSession.main_pool.

        Args:
            worker_ip: Miner identifier (worker:ip format)

        Returns:
            True if published successfully, False otherwise
        """
        if not self.redis:
            logger.error("Redis not connected")
            return False

        try:
            message = json.dumps(
                {
                    "type": "switch_default",
                    "worker_ip": worker_ip,
                    **({"session_id": session_id} if session_id else {}),
                }
            )
            await self.redis.publish(self.channel, message)
            logger.info(f"Published switch_default command for {worker_ip}")
            return True
        except Exception as e:
            logger.error(f"Failed to publish switch_default: {e}")
            return False

    async def close(self) -> None:
        """Close Redis connection."""
        if self.redis:
            await self.redis.close()
            logger.info("Redis publisher connection closed")


class RedisClient:
    """
    General Redis client for key-value operations.

    Used for storing miner heartbeat data as JSON in Redis.
    """

    def __init__(self):
        self.redis: Optional[redis.Redis] = None

    async def connect(self) -> bool:
        """
        Connect to Redis server.

        Returns:
            True if connected successfully, False otherwise
        """
        redis_host = os.getenv("REDIS_HOST", "localhost")
        redis_port = int(os.getenv("REDIS_PORT", "6379"))

        try:
            self.redis = redis.Redis(
                host=redis_host,
                port=redis_port,
                decode_responses=True,
            )
            # Test connection
            await self.redis.ping()
            logger.info(f"Connected to Redis at {redis_host}:{redis_port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            return False

    async def ensure_connected(self) -> bool:
        """
        Ensure Redis connection is alive, reconnect if needed.

        Returns:
            True if connected (or reconnected successfully), False otherwise
        """
        if not self.redis:
            return await self.connect()
        try:
            await self.redis.ping()
            return True
        except Exception:
            # Connection is dead, try to reconnect
            try:
                await self.close()
            except Exception:
                pass
            return await self.connect()

    async def ensure_order_share_group(self) -> bool:
        """
        Ensure the order share processing consumer group exists.

        Returns:
            True if the group exists or was created successfully.
        """
        if not self.redis:
            return False

        try:
            await self.redis.xgroup_create(
                ORDER_SHARE_STREAM,
                ORDER_SHARE_GROUP,
                id="$",
                mkstream=True,
            )
            logger.info(
                "Created Redis stream group '%s' on '%s'",
                ORDER_SHARE_GROUP,
                ORDER_SHARE_STREAM,
            )
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                logger.error("Failed to create Redis stream group: %s", e)
                return False
        return True

    async def publish_order_share(
        self,
        order_id: str,
        difficulty: float,
        worker: Optional[str] = None,
        share_timestamp: Optional[datetime] = None,
    ) -> bool:
        """
        Publish an accepted share to the order share stream.

        Args:
            order_id: Marketplace order identifier
            difficulty: Actual share difficulty
            worker: Optional worker name
            share_timestamp: Optional share timestamp
        """
        if not self.redis:
            return False

        ts = share_timestamp or datetime.now(timezone.utc)
        ts_value = ts.isoformat() if isinstance(ts, datetime) else str(ts)

        data = {
            "order_id": order_id,
            "difficulty": str(difficulty),
            "ts": ts_value,
        }
        if worker:
            data["worker"] = worker

        try:
            await self.redis.xadd(
                ORDER_SHARE_STREAM,
                data,
                maxlen=ORDER_SHARE_STREAM_MAXLEN,
                approximate=True,
            )
            return True
        except Exception as e:
            logger.error("Failed to publish order share: %s", e)
            return False

    async def read_order_share_batch(
        self, count: int, block_ms: int, read_id: str = ">"
    ) -> list[tuple[str, dict]]:
        """
        Read a batch of order share messages for this consumer.
        """
        if not self.redis:
            return []

        try:
            response = await self.redis.xreadgroup(
                ORDER_SHARE_GROUP,
                ORDER_SHARE_CONSUMER,
                {ORDER_SHARE_STREAM: read_id},
                count=count,
                block=block_ms,
            )
        except Exception as e:
            logger.error("Failed to read order share stream: %s", e)
            return []

        messages: list[tuple[str, dict]] = []
        for _, entries in response:
            for message_id, data in entries:
                messages.append((message_id, data))
        return messages

    async def ack_order_shares(self, message_ids: list[str]) -> bool:
        """
        Acknowledge processed order share messages.
        """
        if not self.redis or not message_ids:
            return False

        try:
            await self.redis.xack(ORDER_SHARE_STREAM, ORDER_SHARE_GROUP, *message_ids)
            return True
        except Exception as e:
            logger.error("Failed to ack order share messages: %s", e)
            return False

    async def set_hashprice_cache(
        self, payload: dict, ttl_seconds: Optional[int] = None
    ) -> bool:
        """
        Cache the latest hashprice payload in Redis.
        """
        if not self.redis:
            return False

        try:
            value = json.dumps(payload)
            ttl = HASHPRICE_CACHE_TTL_SECONDS if ttl_seconds is None else ttl_seconds
            if ttl and ttl > 0:
                await self.redis.set(HASHPRICE_CACHE_KEY, value, ex=ttl)
            else:
                await self.redis.set(HASHPRICE_CACHE_KEY, value)
            return True
        except Exception as e:
            logger.error("Failed to set hashprice cache: %s", e)
            return False

    async def get_hashprice_cache(self) -> Optional[dict]:
        """
        Read the latest hashprice payload from Redis.
        """
        if not self.redis:
            return None

        try:
            value = await self.redis.get(HASHPRICE_CACHE_KEY)
            if not value:
                return None
            return json.loads(value)
        except Exception as e:
            logger.error("Failed to get hashprice cache: %s", e)
            return None

    async def get_connected_miners(self) -> list[str]:
        """
        Get all connected miners across all proxy instances.

        Reads from Redis maintained by each proxy's heartbeat loop.

        Returns:
            List of worker_ip identifiers
        """
        if not self.redis:
            return []

        try:
            miners = set()
            proxy_keys = await self.redis.keys("proxy:*:miners")

            for key in proxy_keys:
                data = await self.redis.get(key)
                if data:
                    miners_dict = json.loads(data)
                    miners.update(miners_dict.keys())

            return list(miners)
        except Exception as e:
            logger.error(f"Failed to get connected miners: {e}")
            return []

    async def update_proxy_miners_with_stats(
        self, proxy_id: str, miners: dict[str, dict], ttl_seconds: int = 30
    ) -> bool:
        """
        Store miners and their stats as a single JSON object.

        Args:
            proxy_id: Unique identifier for this proxy instance
            miners: Dict mapping worker_ip to stats dict
            ttl_seconds: Time-to-live for the key (default 30s)

        Returns:
            True if updated successfully, False otherwise
        """
        if not self.redis:
            return False

        redis_key = f"proxy:{proxy_id}:miners"

        try:
            if miners:
                await self.redis.set(redis_key, json.dumps(miners), ex=ttl_seconds)
            else:
                await self.redis.delete(redis_key)
            return True
        except Exception as e:
            logger.error(f"Failed to update miners with stats: {e}")
            return False

    async def get_all_miners_with_stats(self) -> dict[str, dict]:
        """
        Get all miners with stats across all proxy instances.

        Returns:
            Dict mapping worker_ip to stats dict
        """
        if not self.redis:
            return {}

        try:
            miners_by_proxy: list[tuple[Optional[str], dict]] = []
            proxy_keys = await self.redis.keys("proxy:*:miners")

            for key in proxy_keys:
                data = await self.redis.get(key)
                if not data:
                    continue

                try:
                    miners = json.loads(data)
                except Exception as e:
                    logger.error(f"Failed to load miners from Redis: {e}")
                    continue

                proxy_id = None
                try:
                    parts = str(key).split(":")
                    if len(parts) >= 3:
                        proxy_id = parts[1]
                except Exception as e:
                    logger.error(f"Failed to parse proxy ID from Redis key: {e}")
                    pass

                miners_by_proxy.append((proxy_id, miners))

            # Aggregate stats per worker_ip
            aggregators: dict[str, MinerGroupStats] = {}
            worker_names: dict[str, list[str]] = {}
            miner_agents: dict[str, list[str]] = {}

            for proxy_id, miners in miners_by_proxy:
                for worker_ip, stats in miners.items():
                    if not isinstance(stats, dict):
                        continue

                    if proxy_id and isinstance(stats.get("sessions"), list):
                        _sessions: list[dict] = []
                        for session in stats.get("sessions") or []:
                            if isinstance(session, dict):
                                session_dict = dict(session)
                                session_dict.setdefault("proxy_id", proxy_id)
                                _sessions.append(session_dict)
                        stats["sessions"] = _sessions

                    if worker_ip not in aggregators:
                        aggregators[worker_ip] = MinerGroupStats()
                        worker_names[worker_ip] = []
                        miner_agents[worker_ip] = []

                    aggregators[worker_ip].merge_from_dict(
                        stats,
                        worker_name_out=worker_names[worker_ip],
                        miner_agent_out=miner_agents[worker_ip],
                    )

            all_miners: dict[str, dict] = {}
            for worker_ip, agg in aggregators.items():
                all_miners[worker_ip] = agg.to_merged_dict(
                    worker_name=worker_names[worker_ip][0]
                    if worker_names[worker_ip]
                    else "",
                    miner_agent=miner_agents[worker_ip][0]
                    if miner_agents[worker_ip]
                    else "",
                )

            return all_miners
        except Exception as e:
            logger.error(f"Failed to get miners with stats: {e}")
            return {}

    async def close(self) -> None:
        """Close Redis connection."""
        if self.redis:
            await self.redis.close()
            logger.info("Redis client connection closed")
