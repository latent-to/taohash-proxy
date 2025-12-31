"""
Redis client utilities for pub/sub communication.

This module provides Redis publisher and subscriber functionality for
coordinating miner pool switches across multiple proxy instances.
"""

import json
import os
from typing import Optional

import redis.asyncio as redis

from .logger import get_logger

logger = get_logger(__name__)

REDIS_CHANNEL = "miner:commands"


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

    async def publish_switch_pool(self, worker_ip: str, pool_config: dict) -> bool:
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
                    "pool_config": pool_config,
                }
            )
            await self.redis.publish(self.channel, message)
            logger.info(f"Published switch_pool command for {worker_ip}")
            return True
        except Exception as e:
            logger.error(f"Failed to publish switch_pool: {e}")
            return False

    async def publish_switch_default(self, worker_ip: str) -> bool:
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
            all_miners = {}
            proxy_keys = await self.redis.keys("proxy:*:miners")

            for key in proxy_keys:
                data = await self.redis.get(key)
                if data:
                    miners = json.loads(data)
                    all_miners.update(miners)

            return all_miners
        except Exception as e:
            logger.error(f"Failed to get miners with stats: {e}")
            return {}

    async def close(self) -> None:
        """Close Redis connection."""
        if self.redis:
            await self.redis.close()
            logger.info("Redis client connection closed")
