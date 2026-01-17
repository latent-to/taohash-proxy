"""
Mining proxy session handler that bridges miners to upstream pools.

This module manages individual miner connections, handling the Stratum protocol
handshake (subscribe/authorize), job distribution, share submission with difficulty
calculation, and state transitions.
"""

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Any, Dict

from ..utils.logger import get_logger, log_stratum_message
from ..protocol.difficulty import calculate_share_difficulty, parse_min_difficulty
from ..monitoring.stats import StatsManager
from .job_queue import JobQueue
from .pool_config import PoolConfig

logger = get_logger(__name__)


class DisconnectAction(Enum):
    """Actions to take when a connection drops during mining session."""

    EXIT = "exit"  # Clean exit, miner left
    SWITCH_TO_MAIN = "switch_to_main"  # Pool dropped, fallback to main
    CONTINUE_SWITCH = "continue_switch"  # Intentional switch in progress


def determine_disconnect_action(
    miner_disconnected: bool,
    pool_disconnected: bool,
    is_switching: bool,
    has_new_pool_config: bool,
    is_on_order_pool: bool,
) -> DisconnectAction:
    """
    Function to determine the action when a connection drops.

    Priority:
    1. Intentional switch in progress -> continue with switch
    2. Miner disconnected -> exit (no point reconnecting)
    3. Pool disconnected while on order pool -> fallback to main
    4. Otherwise -> exit

    Args:
        miner_disconnected: True if miner->pool task ended
        pool_disconnected: True if pool->miner task ended
        is_switching: True if a pool switch is in progress
        has_new_pool_config: True if new_pool_config is set
        is_on_order_pool: True if currently on an order pool (not main)

    Returns:
        DisconnectAction: Action to take based on the disconnect state
    """
    if is_switching and has_new_pool_config:
        return DisconnectAction.CONTINUE_SWITCH

    if miner_disconnected:
        return DisconnectAction.EXIT

    if pool_disconnected and is_on_order_pool and not is_switching:
        return DisconnectAction.SWITCH_TO_MAIN

    return DisconnectAction.EXIT


class MinerSession:
    """
    Manages a single miner's connection and proxies to upstream pool.

    Handles Stratum protocol messages, tracks mining jobs, calculates share
    difficulty, and enforces minimum difficulty requirements.
    """

    def __init__(
        self,
        miner_reader: asyncio.StreamReader,
        miner_writer: asyncio.StreamWriter,
        pool_config: PoolConfig,
        stats_manager: StatsManager,
    ):
        """
        Initialize miner session.

        Args:
            miner_reader: Stream reader for miner connection
            miner_writer: Stream writer for miner connection
            pool_config: Pool configuration (host, port, user, password, label)
            stats_manager: Stats tracking instance
        """
        self.miner_reader = miner_reader
        self.miner_writer = miner_writer

        # Pool connection
        self.pool_reader: Optional[asyncio.StreamReader] = None
        self.pool_writer: Optional[asyncio.StreamWriter] = None

        # Pool configs
        # Main pool is the default TAOHash pool, the miner connected to (immutable).
        # Current pool changes when a pool switch is requested (mutable).
        self.main_pool = pool_config
        self.current_pool = pool_config

        # Miner identification
        self.peer = miner_writer.get_extra_info("peername")
        self.miner_id = f"{self.peer[0]}:{self.peer[1]}" if self.peer else "unknown"
        # Ephemeral per-TCP-connection id.
        # Used to track multiple concurrent sessions that share the same registry_key (worker_name:ip).
        self.session_id: str = uuid.uuid4().hex

        # Stats tracking
        self.stats = stats_manager.register_miner(self.peer, pool_config.label)
        self.worker_name: Optional[str] = None
        self.miner_agent: str = ""

        # Difficulty management
        self.min_difficulty: Optional[int] = None
        self.pool_difficulty: Optional[float] = None

        # Protocol tracking
        self.extranonce1: Optional[str] = None
        self.extranonce2_size: Optional[int] = None
        self.subscribe_id: Optional[int] = None
        self.pending_submits: Dict[int, dict] = {}
        self.subscribe_params: list = []

        # Job tracking
        self.jobs = JobQueue(max_size=25)
        self.db = None
        self.order_share_publisher = None

        # Switching state
        self.is_switching = False
        self.new_pool_config: Optional[PoolConfig] = None
        self._switch_task: Optional[asyncio.Task] = None
        self._switch_lock = asyncio.Lock()

        self.on_authorized_callback = None

        logger.info(f"[{self.miner_id}] Proxy session initialized")

    def get_session_id(self) -> str:
        """Return the ephemeral session id for this TCP connection."""

        return self.session_id

    def get_registry_key(self) -> Optional[str]:
        """
        Get unique key for miner registry (worker:ip format).

        Returns:
            Registry key in format "worker_name:ip_address" or None if not available
        """
        if self.worker_name and self.peer:
            return f"{self.worker_name}:{self.peer[0]}"
        return None

    def _is_on_order_pool(self) -> bool:
        """
        Check if currently connected to an order's pool and not the default pool.

        Returns:
            True if on order pool, False if on main (original) pool
        """
        return self.current_pool != self.main_pool

    def _clear_pool_state(self) -> None:
        """
        Clear all pool-specific state when switching pools.
        """
        self.jobs.clear()
        self.pending_submits.clear()
        self.extranonce1 = None
        self.extranonce2_size = None
        self.pool_difficulty = None
        logger.debug(f"[{self.miner_id}] Cleared stale pool state")

    def _apply_pool_config(self, pool_config: PoolConfig) -> None:
        """
        Apply pool configuration to current session.

        Args:
            pool_config: PoolConfig instance
        """
        self.current_pool = pool_config
        logger.debug(
            f"[{self.miner_id}] Applied pool config: {pool_config.connection_string} ({pool_config.label})"
        )

    async def _validate_pool(self, pool_config: PoolConfig) -> bool:
        """
        Validate that a pool is reachable before switching.

        Args:
            pool_config: Pool configuration

        Returns:
            True if pool is reachable, False otherwise
        """
        try:
            logger.debug(
                f"[{self.miner_id}] Validating pool {pool_config.connection_string}"
            )
            _, test_writer = await asyncio.wait_for(
                asyncio.open_connection(pool_config.host, pool_config.port),
                timeout=3.0,
            )
            test_writer.close()
            await test_writer.wait_closed()
            logger.info(
                f"[{self.miner_id}] Pool validation successful for {pool_config.connection_string}"
            )
            return True
        except asyncio.TimeoutError:
            logger.error(
                f"[{self.miner_id}] Pool validation timed out (3s) for {pool_config.connection_string}"
            )
            return False
        except Exception as e:
            logger.error(
                f"[{self.miner_id}] Pool validation failed for {pool_config.connection_string}: {e}"
            )
            return False

    async def _validate_and_switch(self, pool_config: PoolConfig) -> bool:
        """
        Background task to validate pool and trigger switch.
        If validation fails, falls back to main pool.

        Args:
            pool_config: Target pool configuration

        Returns:
            True if requested pool validated and switch triggered
            False if validation failed (stayed put or fell back to main)
        """
        try:
            logger.info(
                f"[{self.miner_id}] Background validation for {pool_config.label} ({pool_config.connection_string})"
            )

            target_config = pool_config
            if not await self._validate_pool(pool_config):
                if not self._is_on_order_pool():
                    logger.info(
                        f"[{self.miner_id}] Pool validation failed for {pool_config.label}, "
                        f"already on main pool {self.main_pool.connection_string}"
                    )
                    return False

                logger.warning(
                    f"[{self.miner_id}] Pool validation failed for {pool_config.label}, "
                    f"falling back to main pool {self.main_pool.connection_string}"
                )
                target_config = self.main_pool
                validation_succeeded = False
            else:
                validation_succeeded = True

            async with self._switch_lock:
                self.new_pool_config = target_config
                self.is_switching = True

                # Trigger disconnect of current pool to initiate switch
                if self.pool_writer:
                    try:
                        self.pool_writer.close()
                        await self.pool_writer.wait_closed()
                        logger.debug(
                            f"[{self.miner_id}] Triggered pool writer close for switch to "
                            f"{target_config.label} ({target_config.connection_string})"
                        )
                    except Exception as e:
                        logger.error(
                            f"[{self.miner_id}] Error closing pool writer for switch: {e}"
                        )
                        return False

            return validation_succeeded

        except Exception as e:
            logger.error(f"[{self.miner_id}] Error in background switch task: {e}")
            return False
        finally:
            self._switch_task = None

    async def run(self):
        """
        Main session loop - manages miner pool connection lifecycle.

        Connects to pool immediately and runs two forwarding loops
        """
        try:
            while True:
                logger.info(
                    f"[{self.miner_id}] Connecting to pool {self.current_pool.connection_string}"
                )
                try:
                    self.pool_reader, self.pool_writer = await asyncio.open_connection(
                        self.current_pool.host, self.current_pool.port
                    )
                    logger.info(f"[{self.miner_id}] Pool connection established")

                except Exception as e:
                    logger.error(
                        f"[{self.miner_id}] Failed to connect to pool {self.current_pool.connection_string}: {e}"
                    )

                    # Case: Failure while switching and on order pool - roll back to the main pool
                    if self.is_switching and self._is_on_order_pool():
                        logger.error(
                            f"[{self.miner_id}] New pool connection failed, "
                            f"falling back to main pool {self.main_pool.connection_string}"
                        )
                        self._clear_pool_state()
                        self._apply_pool_config(self.main_pool)
                        self.new_pool_config = None

                        continue  # Retry with main pool

                    # Case: Normal connection failure
                    break

                # Case: Initiated switch - perform handshake
                async with self._switch_lock:
                    if self.is_switching:
                        await self._perform_pool_handshake()
                        self.is_switching = False
                        self.new_pool_config = None

                miner_to_pool = asyncio.create_task(self._forward_miner_to_pool())
                pool_to_miner = asyncio.create_task(self._forward_pool_to_miner())

                done, pending = await asyncio.wait(
                    [miner_to_pool, pool_to_miner], return_when=asyncio.FIRST_COMPLETED
                )

                # Track dropped connection
                miner_disconnected = miner_to_pool in done
                pool_disconnected = pool_to_miner in done

                for task in done:
                    try:
                        if task.exception():
                            logger.error(
                                f"[{self.miner_id}] Task failed: {task.exception()}"
                            )
                    except asyncio.CancelledError:
                        pass

                # Cancel pending tasks
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                # Determine action based on disconnect state
                async with self._switch_lock:
                    action = determine_disconnect_action(
                        miner_disconnected=miner_disconnected,
                        pool_disconnected=pool_disconnected,
                        is_switching=self.is_switching,
                        has_new_pool_config=self.new_pool_config is not None,
                        is_on_order_pool=self._is_on_order_pool(),
                    )

                if action == DisconnectAction.CONTINUE_SWITCH:
                    logger.info(f"[{self.miner_id}] Switching upstream pool...")

                    if self.pool_writer:
                        try:
                            self.pool_writer.close()
                            await self.pool_writer.wait_closed()
                        except Exception as e:
                            logger.error(f"Error closing old pool: {e}")

                    async with self._switch_lock:
                        self._clear_pool_state()
                        self._apply_pool_config(self.new_pool_config)

                    continue

                elif action == DisconnectAction.SWITCH_TO_MAIN:
                    logger.warning(
                        f"[{self.miner_id}] Order pool {self.current_pool.label} ({self.current_pool.connection_string}) "
                        f"disconnected unexpectedly. Switching to main pool..."
                    )

                    async with self._switch_lock:
                        self._clear_pool_state()
                        self._apply_pool_config(self.main_pool)
                        self.is_switching = True
                        self.new_pool_config = None

                    continue

                else:  # DisconnectAction.EXIT
                    self.is_switching = False
                    break

        except Exception as e:
            logger.error(f"[{self.miner_id}] Session error: {e}")
        finally:
            if not self.is_switching:
                await self._cleanup()

    async def request_pool_switch(
        self, pool_config: PoolConfig
    ) -> Optional[asyncio.Task]:
        """
        Request to switch the upstream pool without disconnecting miner.
        Spawns a background task to validate and switch pools.

        Args:
            pool_config: Target pool configuration

        Returns:
            Task[bool] that resolves to True if validation succeeded, False otherwise
            None if switch already in progress
        """
        logger.info(
            f"[{self.miner_id}] Requesting switch to {pool_config.label} ({pool_config.connection_string})"
        )

        async with self._switch_lock:
            if self._switch_task and not self._switch_task.done():
                logger.warning(
                    f"[{self.miner_id}] Switch already in progress, ignoring new request"
                )
                return None

            self._switch_task = asyncio.create_task(
                self._validate_and_switch(pool_config)
            )
            logger.debug(f"[{self.miner_id}] Spawned background switch task")

        return self._switch_task

    async def _perform_pool_handshake(self):
        """
        Re-establish state with new pool (Subscribe + Authorize).
        """
        logger.info(f"[{self.miner_id}] Performing handshake with new pool")

        # 1. Subscribe
        msg_id = self.subscribe_id or 1
        subscribe_msg = {
            "id": msg_id,
            "method": "mining.subscribe",
            "params": self.subscribe_params,
        }

        log_stratum_message(
            logger, subscribe_msg, prefix=f"[{self.miner_id}] Handshake: Subscribe"
        )
        data = json.dumps(subscribe_msg).encode() + b"\n"
        self.pool_writer.write(data)
        await self.pool_writer.drain()

        # 2. Authorize
        auth_id = msg_id + 1
        auth_msg = {
            "id": auth_id,
            "method": "mining.authorize",
            "params": [self.current_pool.user, self.current_pool.password],
        }

        log_stratum_message(
            logger, auth_msg, prefix=f"[{self.miner_id}] Handshake: Authorize"
        )
        data = json.dumps(auth_msg).encode() + b"\n"
        self.pool_writer.write(data)
        await self.pool_writer.drain()

    async def _forward_miner_to_pool(self):
        """
        Forward messages from miner to pool.

        Tracks:
        - mining.subscribe: Track ID for extranonce extraction
        - mining.authorize: Update credentials and parse min difficulty
        - mining.submit: Track for stats and update worker name
        """
        try:
            while True:
                try:
                    line = await self.miner_reader.readline()
                except ConnectionResetError:
                    logger.info(f"[{self.miner_id}] Miner connection reset")
                    break

                if not line:
                    logger.info(
                        f"[{self.miner_id}] {self.worker_name} - Miner disconnected"
                    )
                    break

                try:
                    message = json.loads(line.decode().strip())
                    log_stratum_message(
                        logger, message, prefix=f"[{self.miner_id}] From miner"
                    )

                    method = message.get("method")
                    msg_id = message.get("id")

                    if method == "mining.subscribe":
                        # For extracting extranonce from response
                        self.subscribe_id = msg_id
                        self.subscribe_params = message.get("params", [])

                        # Extract and store miner agent
                        if (
                            self.subscribe_params
                            and len(self.subscribe_params) > 0
                            and self.subscribe_params[0]
                        ):
                            self.miner_agent = str(self.subscribe_params[0])

                        if self.stats:
                            self.stats.set_miner_agent(self.subscribe_params)

                        logger.debug(
                            f"[{self.miner_id}] Tracking subscribe request ID: {msg_id}"
                        )

                    elif method == "mining.authorize":
                        params = message.get("params", [])
                        if len(params) >= 2:
                            self.worker_name = params[0]
                            miner_password = params[1]

                            # Parse min difficulty from password
                            _, min_diff = parse_min_difficulty(miner_password)
                            if min_diff is not None:
                                self.min_difficulty = min_diff
                                logger.info(
                                    f"[{self.miner_id}] Set min_difficulty={min_diff} from miner password"
                                )

                            self.stats.worker_name = self.worker_name
                            logger.info(
                                f"[{self.miner_id}] Worker: {self.worker_name} ({self.miner_agent})"
                            )

                            if self.on_authorized_callback:
                                try:
                                    await self.on_authorized_callback(self)
                                except Exception as e:
                                    logger.error(
                                        f"[{self.miner_id}] Auth callback failed: {e}"
                                    )

                        message["params"] = [
                            self.current_pool.user,
                            self.current_pool.password,
                        ]
                        logger.debug(
                            f"[{self.miner_id}] Replaced credentials for pool auth"
                        )

                    elif method == "mining.submit":
                        share_received_at = datetime.now(timezone.utc)
                        params = message.get("params", [])
                        if len(params) >= 5 and msg_id is not None:
                            submit_data = {
                                "worker": params[0],
                                "job_id": params[1],
                                "extranonce2": params[2],
                                "ntime": params[3],
                                "nonce": params[4],
                                "version": params[5] if len(params) > 5 else None,
                                "received_at": share_received_at,
                            }
                            self.pending_submits[msg_id] = submit_data
                            logger.debug(
                                f"[{self.miner_id}] Tracking submit {msg_id} for job {submit_data['job_id']}"
                            )

                        if params:
                            params[0] = self.current_pool.user

                    elif method == "mining.suggest_difficulty":
                        params = message.get("params", [])
                        if params:
                            logger.info(
                                f"[{self.miner_id} ({self.miner_agent})] Miner suggested difficulty: {params[0]}"
                            )

                    log_stratum_message(
                        logger, message, prefix=f"[{self.miner_id}] To pool"
                    )
                    data = json.dumps(message).encode() + b"\n"
                    self.pool_writer.write(data)
                    await self.pool_writer.drain()

                except json.JSONDecodeError as e:
                    logger.warning(f"[{self.miner_id}] Invalid JSON from miner: {e}")
                except Exception as e:
                    logger.error(f"[{self.miner_id}] Error forwarding to pool: {e}")

        except asyncio.CancelledError:
            logger.debug(f"[{self.miner_id}] Miner to pool forwarding cancelled")
            raise

    async def _forward_pool_to_miner(self):
        """
        Forward messages from pool to miner with minimal interception.

        Intercepts:
        - Subscribe response: Extract extranonce
        - mining.notify: Store jobs for difficulty calculation
        - mining.set_difficulty: Enforce minimum if configured
        - Submit responses: Update stats
        """
        try:
            while True:
                if not self.pool_reader:
                    break

                line = await self.pool_reader.readline()
                if not line:
                    logger.info(f"[{self.miner_id}] Pool disconnected")
                    try:
                        if self.stats:
                            self.stats.pool_disconnects += 1
                    except Exception:
                        pass
                    break

                try:
                    message = json.loads(line.decode().strip())
                    log_stratum_message(
                        logger, message, prefix=f"[{self.miner_id}] From pool"
                    )

                    method = message.get("method")
                    msg_id = message.get("id")

                    if msg_id == self.subscribe_id:
                        result = message.get("result", [])
                        if len(result) >= 3:
                            self.extranonce1 = result[1]
                            self.extranonce2_size = result[2]
                            logger.info(
                                f"[{self.miner_id} ({self.miner_agent})] Got extranonce1={self.extranonce1}, "
                                f"extranonce2_size={self.extranonce2_size}"
                            )

                    elif method == "mining.notify":
                        params = message.get("params", [])
                        if len(params) >= 9:
                            try:
                                if self.stats:
                                    self.stats.last_notify_time = time.time()
                            except Exception:
                                pass
                            job_id = params[0]
                            job_data = {
                                "job_id": job_id,
                                "prevhash": params[1],
                                "coinb1": params[2],
                                "coinb2": params[3],
                                "merkle_branches": params[4],
                                "version": params[5],
                                "nbits": params[6],
                                "ntime": params[7],
                                "clean_jobs": params[8],
                            }
                            self.jobs.add_job(job_id, job_data)
                            logger.debug(f"[{self.miner_id}] Stored job {job_id}")

                    elif method == "mining.set_difficulty":
                        params = message.get("params", [])
                        if params:
                            pool_diff = float(params[0])
                            self.pool_difficulty = pool_diff
                            self.stats.pool_difficulty = pool_diff

                            effective_diff = pool_diff
                            if self.min_difficulty is not None:
                                effective_diff = max(pool_diff, self.min_difficulty)
                                if effective_diff != pool_diff:
                                    logger.info(
                                        f"[{self.miner_id}] Enforcing min difficulty: "
                                        f"pool={pool_diff}, min={self.min_difficulty}, "
                                        f"effective={effective_diff}"
                                    )
                                    message["params"][0] = effective_diff

                            self.stats.update_difficulty(effective_diff)

                    elif msg_id in self.pending_submits:
                        await self._handle_submit_response(message, msg_id)

                    # Forward to miner
                    log_stratum_message(
                        logger, message, prefix=f"[{self.miner_id}] To miner"
                    )
                    data = json.dumps(message).encode() + b"\n"
                    self.miner_writer.write(data)
                    await self.miner_writer.drain()

                except json.JSONDecodeError as e:
                    logger.warning(f"[{self.miner_id}] Invalid JSON from pool: {e}")
                except Exception as e:
                    logger.error(f"[{self.miner_id}] Error forwarding to miner: {e}")

        except asyncio.CancelledError:
            logger.debug(f"[{self.miner_id}] Pool to miner forwarding cancelled")
            raise

    async def _handle_submit_response(self, message: dict[str, Any], msg_id: int):
        """
        Process pool's response to a share submission.

        Updates stats and optionally stores to database.
        """
        submit_data = self.pending_submits.pop(msg_id)

        result = message.get("result")
        error = message.get("error")
        reject_reason = message.get("reject-reason")

        if error is None and reject_reason:
            error = [23, reject_reason]

        accepted = result is True and error is None and reject_reason is None

        # Calculate actual share difficulty
        actual_difficulty = 0.0
        job_data = self.jobs.get_job(submit_data["job_id"])

        if job_data:
            try:
                actual_difficulty, block_hash = calculate_share_difficulty(
                    job=job_data,
                    extranonce1=self.extranonce1,
                    extranonce2=submit_data["extranonce2"],
                    ntime=submit_data["ntime"],
                    nonce=submit_data["nonce"],
                    version=submit_data.get("version"),
                )
            except Exception as e:
                logger.error(f"[{self.miner_id}] Difficulty calculation error: {e}")

        self.stats.record_share(
            accepted=accepted,
            difficulty=self.stats.difficulty,
            share_difficulty=actual_difficulty,
            pool=self.current_pool.connection_string,
            error=json.dumps(error) if error else None,
        )

        if accepted and self.db and actual_difficulty > 0:
            block_hash_value = block_hash if "block_hash" in locals() else ""
            share_timestamp = submit_data.get("received_at")
            miner_id = self.miner_id
            worker = submit_data["worker"]
            pool = self.current_pool.connection_string
            pool_label = self.current_pool.label
            pool_difficulty = self.stats.difficulty
            pool_requested_difficulty = self.pool_difficulty

            async def _insert_share() -> None:
                try:
                    await self.db.insert_share(
                        miner=miner_id,
                        worker=worker,
                        pool=pool,
                        pool_difficulty=pool_difficulty,
                        actual_difficulty=actual_difficulty,
                        block_hash=block_hash_value,
                        pool_requested_difficulty=pool_requested_difficulty,
                        pool_label=pool_label,
                        share_timestamp=share_timestamp,
                    )
                except Exception as e:
                    logger.error(f"[{miner_id}] Failed to insert share: {e}")

            asyncio.create_task(_insert_share())

            try:
                if self.order_share_publisher and self._is_on_order_pool():
                    published = self.order_share_publisher.enqueue_share(
                        order_id=self.current_pool.label,
                        difficulty=actual_difficulty,
                        worker=submit_data.get("worker"),
                        share_timestamp=submit_data.get("received_at"),
                    )
                    if not published:
                        logger.warning(
                            f"[{self.miner_id}] Failed to publish share for order {self.current_pool.label}"
                        )
            except Exception as e:
                logger.error(f"[{self.miner_id}] Failed to publish share: {e}")

        if accepted:
            logger.info(
                f"[{self.miner_id} ({self.miner_agent})] {submit_data['worker']} - Share accepted: "
                f"diff={actual_difficulty:.2f}"
            )
        else:
            self._insert_rejected_share(
                submit_data, actual_difficulty, error, reject_reason
            )
            reason = "unknown"
            if error and isinstance(error, list) and len(error) > 1:
                reason = error[1]
            elif reject_reason:
                reason = reject_reason
            logger.info(
                f"[{self.miner_id} ({self.miner_agent})] {submit_data['worker']} - Share rejected ({reason}): "
                f"diff={actual_difficulty:.2f}"
            )

        # Check acceptance ratio every 250 shares
        total_shares = self.stats.accepted + self.stats.rejected
        if total_shares > 0 and total_shares % 250 == 0:
            acceptance_rate = (self.stats.accepted / total_shares) * 100

            if acceptance_rate < 30:
                logger.warning(
                    f"[{self.miner_id} ({self.miner_agent})] {submit_data.get('worker', 'unknown')} - "
                    f"Disconnecting: {acceptance_rate:.1f}% acceptance rate "
                    f"({self.stats.accepted}/{total_shares} accepted)"
                )

                # Close the miner connection
                self.miner_writer.close()
                await self.miner_writer.wait_closed()

    def _insert_rejected_share(
        self,
        submit_data: dict[str, Any],
        actual_difficulty: float,
        error: Any,
        reject_reason: Optional[str],
    ) -> None:
        """Add rejected share to the database."""
        if not self.db:
            return

        share_timestamp = submit_data.get("received_at")
        miner_id = self.miner_id
        worker = submit_data.get("worker", "")
        pool = self.current_pool.connection_string
        pool_label = self.current_pool.label
        pool_difficulty = self.stats.difficulty
        pool_requested_difficulty = self.pool_difficulty or 0.0
        min_difficulty = self.min_difficulty or 0.0
        job_id = submit_data.get("job_id", "")
        extranonce2 = submit_data.get("extranonce2", "")
        ntime = submit_data.get("ntime", "")
        nonce = submit_data.get("nonce", "")
        version = submit_data.get("version")

        error_code = 0
        error_message = ""
        if isinstance(error, list):
            if error:
                try:
                    error_code = int(error[0])
                except (TypeError, ValueError):
                    error_code = 0
            if len(error) > 1 and error[1] is not None:
                error_message = str(error[1])
        elif error is not None:
            error_message = str(error)

        reject_reason_value = str(reject_reason) if reject_reason else ""
        error_raw = json.dumps(
            {"error": error, "reject_reason": reject_reason},
            default=str,
        )

        async def _insert_rejected_share() -> None:
            try:
                await self.db.insert_rejected_share(
                    miner=miner_id,
                    worker=worker,
                    pool=pool,
                    pool_difficulty=pool_difficulty,
                    actual_difficulty=actual_difficulty,
                    pool_label=pool_label,
                    job_id=job_id,
                    extranonce2=extranonce2,
                    ntime=ntime,
                    nonce=nonce,
                    version=version,
                    share_timestamp=share_timestamp,
                    pool_requested_difficulty=pool_requested_difficulty,
                    min_difficulty=min_difficulty,
                    error_code=error_code,
                    error_message=error_message,
                    reject_reason=reject_reason_value,
                    error_raw=error_raw,
                )
            except Exception as e:
                logger.error(f"[{miner_id}] Failed to insert rejected share: {e}")

        asyncio.create_task(_insert_rejected_share())

    async def _cleanup(self):
        """Clean up resources on disconnect."""
        logger.info(f"[{self.miner_id}] Cleaning up session")

        # Close miner connection
        try:
            self.miner_writer.close()
            try:
                await self.miner_writer.wait_closed()
            except ConnectionResetError:
                logger.info(f"[{self.miner_id}] Miner already closed connection")
                pass
        except Exception:
            pass

        # Close pool connection
        if self.pool_writer:
            try:
                self.pool_writer.close()
                try:
                    await self.pool_writer.wait_closed()
                except ConnectionResetError:
                    logger.info(f"[{self.miner_id}] Pool already closed connection")
                    pass
            except Exception:
                pass

        logger.info(f"[{self.miner_id}] Session cleaned up")
