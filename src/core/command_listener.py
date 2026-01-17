"""
Redis command listener for receiving pool switch commands from the API.

This module subscribes to Redis pub/sub channel and executes pool switch
commands for miners that are connected to this proxy instance.
"""

import asyncio
import json
import os
from typing import TYPE_CHECKING, Any, Optional

import redis.asyncio as redis

from ..utils.logger import get_logger
from .pool_config import PoolConfig

if TYPE_CHECKING:
    from .miner_session import MinerSession

logger = get_logger(__name__)

REDIS_CHANNEL = "miner:commands"


class CommandListener:
    """
    Redis subscriber for receiving miner control commands.

    Listens to the miner:commands channel and executes pool switches
    for miners in the local registry.
    """

    def __init__(
        self,
        miner_registry: dict[str, dict[str, "MinerSession"]],
        miner_sessions_by_id: dict[str, "MinerSession"],
        miner_registry_lock: asyncio.Lock,
        config: dict,
    ):
        """
        Initialize the command listener.

        Args:
            miner_registry: Local registry of connected miners
            miner_registry_lock: Lock for thread-safe registry access
            config: Pool configuration dict
        """
        self.redis: Optional[redis.Redis] = None
        self.pubsub: Optional[redis.client.PubSub] = None
        self.channel = REDIS_CHANNEL
        self.miner_registry = miner_registry
        self.miner_sessions_by_id = miner_sessions_by_id
        self.miner_registry_lock = miner_registry_lock
        self.config = config
        self._running = False

    async def connect(self) -> bool:
        """
        Connect to Redis and subscribe to the command channel.

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

            self.pubsub = self.redis.pubsub()
            await self.pubsub.subscribe(self.channel)

            logger.info(f"Subscribed to Redis channel: {self.channel}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            return False

    async def listen(self) -> None:
        """
        Main listener loop with automatic reconnection.

        Runs forever, processing commands. If Redis connection is lost,
        automatically reconnects with a 5 second delay.

        Should be run as a background task.
        """
        self._running = True

        while self._running:
            try:
                if not self.pubsub:
                    connected = await self.connect()
                    if not connected:
                        logger.error("Failed to connect to Redis, retrying in 5s...")
                        await asyncio.sleep(5)
                        continue

                logger.info("Command listener started")

                async for message in self.pubsub.listen():
                    if not self._running:
                        break

                    if message["type"] != "message":
                        continue

                    try:
                        data = json.loads(message["data"])
                        await self._handle_command(data)
                    except json.JSONDecodeError as e:
                        logger.error(f"Invalid JSON in command: {e}")
                    except Exception as e:
                        logger.error(f"Error handling command: {e}")

            except asyncio.CancelledError:
                logger.info("Command listener cancelled")
                raise
            except Exception as e:
                logger.error(f"Command listener error: {e}, reconnecting in 5s...")
                self.pubsub = None
                self.redis = None
                await asyncio.sleep(5)

    async def _handle_command(self, data: dict[str, Any]) -> None:
        """
        Handle a single command from Redis.

        Args:
            data: Command data dict with type, worker_ip, and command-specific fields
        """
        worker_ip = data.get("worker_ip")
        session_id = data.get("session_id")
        cmd_type = data.get("type")

        if not worker_ip or not cmd_type:
            logger.warning(f"Invalid command format: {data}")
            return

        async with self.miner_registry_lock:
            targets: list["MinerSession"] = []

            if session_id:
                session = self.miner_sessions_by_id.get(str(session_id))
                if session:
                    registry_key = session.get_registry_key() or ""
                    if registry_key and registry_key != str(worker_ip):
                        logger.warning(
                            f"Command session_id={session_id} resolves to registry_key={registry_key} (command worker_ip={worker_ip})"
                        )
                    targets = [session]
            else:
                registry_key = self.miner_registry.get(str(worker_ip))
                if registry_key:
                    targets = list(registry_key.values())

        if not targets:
            return

        logger.info(
            f"Received {cmd_type} command for {worker_ip} (sessions={len(targets)})",
        )

        if cmd_type == "switch_pool":
            results = await asyncio.gather(
                *(self._handle_switch_pool(session, data) for session in targets),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, Exception):
                    logger.error("Error handling switch_pool: %s", result)

        elif cmd_type == "switch_default":
            results = await asyncio.gather(
                *(self._handle_switch_default(session) for session in targets),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, Exception):
                    logger.error("Error handling switch_default: %s", result)
        else:
            logger.warning(f"Unknown command type: {cmd_type}")

    async def _handle_switch_pool(
        self, session: "MinerSession", data: dict[str, Any]
    ) -> None:
        """
        Handle a switch_pool command.

        Args:
            session: The miner session to switch
            data: Command data with pool_config
        """
        pool_config_data = data.get("pool_config", {})

        if not pool_config_data:
            logger.error("switch_pool command missing pool_config")
            return

        required = {"host", "port", "user"}
        if not required.issubset(pool_config_data.keys()):
            logger.error(
                f"switch_pool command missing required fields: {required - set(pool_config_data.keys())}"
            )
            return

        try:
            pool_config = PoolConfig.from_dict(pool_config_data)
        except (KeyError, ValueError) as e:
            logger.error(f"Invalid pool_config in switch_pool command: {e}")
            return

        logger.info(
            f"Switching {session.get_registry_key()} to {pool_config.connection_string}"
        )

        task = await session.request_pool_switch(pool_config)
        if not task:
            logger.error(
                f"Failed to request pool switch for {session.get_registry_key()} (switch already in progress)"
            )
            return

        validation_success = await task
        if validation_success:
            logger.info(
                f"Pool switch validated and triggered for {session.get_registry_key()}"
            )
        else:
            logger.warning(
                f"Pool validation failed for {session.get_registry_key()}, fell back to main pool or stayed put"
            )

    async def _handle_switch_default(self, session: "MinerSession") -> None:
        """
        Handle a switch_default command.

        Switches the miner back to their main_pool (the pool they originally
        connected to based on their connection port).

        Args:
            session: The miner session to switch back to default
        """
        pool_config = session.main_pool

        logger.info(
            f"Switching {session.get_registry_key()} back to main pool ({pool_config.label})"
        )

        task = await session.request_pool_switch(pool_config)
        if not task:
            logger.error(
                f"Failed to request main pool switch for {session.get_registry_key()} (switch already in progress)"
            )
            return

        validation_success = await task
        if validation_success:
            logger.info(
                f"Main pool switch validated and triggered for {session.get_registry_key()}"
            )
        else:
            logger.warning(
                f"Main pool validation failed for {session.get_registry_key()}, stayed on current pool"
            )

    async def close(self) -> None:
        """Close Redis connection and stop listening."""
        self._running = False

        if self.pubsub:
            await self.pubsub.unsubscribe(self.channel)
            await self.pubsub.close()

        if self.redis:
            await self.redis.close()

        logger.info("Command listener closed")
