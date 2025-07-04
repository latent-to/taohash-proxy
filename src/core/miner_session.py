"""
Mining proxy session handler that bridges miners to upstream pools.

This module manages individual miner connections, handling the Stratum protocol
handshake (subscribe/authorize), job distribution, share submission with difficulty
calculation, and state transitions.
"""

import asyncio
import time
import json
from typing import Optional, Any

from ..utils.logger import get_logger, log_stratum_message
from ..protocol.difficulty import calculate_share_difficulty, parse_min_difficulty
from .pool_session import PoolSession
from ..monitoring.stats import StatsManager
from .miner_state import MinerStateMachine, MinerState
from .job_queue import JobQueue

logger = get_logger(__name__)


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
        pool_host: str,
        pool_port: int,
        pool_user: str,
        pool_pass: str,
        stats_manager: StatsManager,
        pool_label: str = "unknown",
    ):
        """
        Initialize miner session.

        Args:
            miner_reader: Stream reader for miner connection
            miner_writer: Stream writer for miner connection
            pool_host: Upstream pool hostname
            pool_port: Upstream pool port
            pool_user: Pool authentication username
            pool_pass: Pool authentication password (may contain d=X for min difficulty)
            stats_manager: Stats tracking instance
        """
        self.miner_reader = miner_reader
        self.miner_writer = miner_writer
        self.pool_host = pool_host
        self.pool_port = pool_port
        self.pool_user = pool_user
        self.pool_pass = pool_pass
        self.min_difficulty: Optional[int] = None
        self.pool_difficulty: Optional[float] = None

        self.peer = miner_writer.get_extra_info("peername")
        self.miner_id = f"{self.peer[0]}:{self.peer[1]}" if self.peer else "unknown"
        self.pool_label = pool_label
        self.stats = stats_manager.register_miner(self.peer, pool_label)
        self.pool_session: Optional[PoolSession] = None
        self.pending_calls: dict[int, Any] = {}

        self.jobs = JobQueue(max_size=25)  # FIFO queue with max 25 jobs
        self.db = None

        self.state_machine = MinerStateMachine(self.miner_id)
        self.state_machine.on_state_change = self._on_state_change

        self.pool_init_data = {
            "extranonce1": None,
            "extranonce2_size": None,
            "subscription_ids": None,
            "initial_difficulty": None,
            "initial_job": None,
        }

        self._initial_pending_requests = []
        self.pending_configure = None
        self.pool_connecting = False
        self.pool_connect_task = None

        logger.info(f"[{self.miner_id}] Miner session initialized")

    async def _handle_initial_miner_requests(self):
        """
        Collect early miner requests before pool connection.
        
        Waits for mining.authorize as the signal for complete handshake,
        then waits additional time for any trailing messages.
        """
        logger.debug(f"[{self.miner_id}] Handling initial miner requests")

        pending_requests = []
        has_authorize = False
        authorize_time = None
        
        AUTHORIZE_TIMEOUT = 0.7
        POST_AUTHORIZE_WAIT = 0.2
        
        start_time = time.time()
        
        while True:
            current_time = time.time()
            elapsed = current_time - start_time
            
            if not has_authorize:
                if elapsed > AUTHORIZE_TIMEOUT:
                    logger.warning(f"[{self.miner_id}] No authorize after {AUTHORIZE_TIMEOUT}s, proceeding anyway")
                    break
                timeout = 0.2
            else:
                if current_time - authorize_time > POST_AUTHORIZE_WAIT:
                    logger.debug(f"[{self.miner_id}] Post-authorize wait complete")
                    break
                timeout = 0.1
            
            try:
                line = await asyncio.wait_for(self.miner_reader.readline(), timeout)
                if not line:
                    break

                message_str = line.decode().strip()
                if not message_str:
                    continue

                try:
                    request = json.loads(message_str)
                    method = request.get("method")
                    req_id = request.get("id")

                    logger.debug(
                        f"[{self.miner_id}] Initial request: method={method}, id={req_id}"
                    )

                    if method == "mining.configure":
                        self.pending_configure = request
                        logger.info(f"[{self.miner_id}] Got configure request at {elapsed:.3f}s")
                        
                        if not self.pool_connecting:
                            self.pool_connecting = True
                            self.pool_connect_task = asyncio.create_task(self._connect_to_pool())
                            logger.debug(f"[{self.miner_id}] Started early pool connection")

                    elif method == "mining.suggest_difficulty":
                        await self._handle_suggest_difficulty(request, req_id)
                        
                    elif method == "mining.authorize":
                        has_authorize = True
                        authorize_time = current_time
                        pending_requests.append(request)
                        logger.debug(f"[{self.miner_id}] Got authorize at {elapsed:.3f}s, waiting {POST_AUTHORIZE_WAIT}s more")
                        
                    else:
                        pending_requests.append(request)

                except json.JSONDecodeError:
                    logger.warning(f"[{self.miner_id}] Invalid JSON in initial request")

            except asyncio.TimeoutError:
                continue

        self._initial_pending_requests = pending_requests

        total_time = time.time() - start_time
        logger.info(
            f"[{self.miner_id}] Collected {len(pending_requests)} pending requests in {total_time:.3f}s"
        )
        
        if not self.pool_connecting:
            self.pool_connecting = True
            self.pool_connect_task = asyncio.create_task(self._connect_to_pool())
            logger.debug(f"[{self.miner_id}] No configure received, starting pool connection")

    async def run(self):
        """
        Main session loop - manages miner pool connection lifecycle.

        Collects early miner requests, connects to pool, then handles
        bidirectional message routing until disconnect.
        """
        try:
            await self._handle_initial_miner_requests()
            
            if self.pool_connect_task:
                await self.pool_connect_task
            else:
                await self._connect_to_pool()

            miner_task = asyncio.create_task(self._handle_miner_messages())
            pool_task = asyncio.create_task(self._handle_pool_messages())

            done, pending = await asyncio.wait(
                [miner_task, pool_task], return_when=asyncio.FIRST_COMPLETED
            )

            for task in pending:
                task.cancel()
            
            for task in pending:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except Exception as e:
            logger.error(f"[{self.miner_id}] Session error: {e}")
            await self.state_machine.handle_error(e, "session_error")
        finally:
            await self._cleanup()

    async def _connect_to_pool(self):
        """
        Connect to upstream pool and retrieve mining parameters.

        Stores extranonce1, extranonce2_size from pool, processes any
        pre-auth messages (difficulty/job), then handles queued miner requests.
        """
        try:
            self.pool_session = await PoolSession.connect(
                self.pool_host, self.pool_port, self.pool_user, self.pool_pass,
                configure_request=self.pending_configure
            )

            # Pool session data
            self.pool_init_data["extranonce1"] = self.pool_session.extranonce1
            self.pool_init_data["extranonce2_size"] = self.pool_session.extranonce2_size
            self.pool_init_data["subscription_ids"] = self.pool_session.subscription_ids

            await self._process_pool_init_messages()
            logger.info(f"[{self.miner_id}] Pool connection established")
            
            if self.pending_configure:
                logger.debug(f"[{self.miner_id}] Pending configure request: {self.pending_configure}")
                if self.pool_session and self.pool_session.configure_response is not None:
                    response = {
                        "id": self.pending_configure.get("id"),
                        "result": self.pool_session.configure_response,
                        "error": None
                    }
                    await self._send_to_miner(response)
                    logger.debug(f"[{self.miner_id}] Sent configure response to miner: {response}")
                else:
                    logger.debug(f"[{self.miner_id}] No configure response from pool")
            else:
                logger.debug(f"[{self.miner_id}] No pending configure request")
            
            await self._process_pending_miner_requests()

        except Exception as e:
            logger.error(f"[{self.miner_id}] Pool connection failed: {e}")
            await self.state_machine.handle_error(e, "pool_connection")
            raise

    async def _process_pool_init_messages(self):
        """
        Extract initial mining params from pre-auth pool messages.

        Stores initial difficulty and first job notification for sending
        to miner after authorization completes.
        """
        if not self.pool_session or not self.pool_session.pre_auth_messages:
            return

        for msg in self.pool_session.pre_auth_messages:
            method = msg.get("method")

            if method == "mining.set_difficulty":
                try:
                    pool_diff = float(msg["params"][0])
                    self.pool_init_data["initial_difficulty"] = pool_diff
                    self.pool_difficulty = pool_diff
                    self.stats.pool_difficulty = pool_diff
                    logger.debug(f"[{self.miner_id}] Got initial difficulty from pool: {pool_diff}")
                except (ValueError, TypeError, IndexError):
                    pass

            elif method == "mining.notify":
                self.pool_init_data["initial_job"] = msg
                # Store job data
                await self._store_job_from_notify(msg)
                logger.debug(f"[{self.miner_id}] Got initial job from pool")

        self.pool_session.pre_auth_messages.clear()

    async def _process_pending_miner_requests(self):
        """
        Process miner requests collected during pool handshake.

        Handles subscribe/authorize that arrived before pool connection
        was ready with extranonce data.
        """
        logger.info(f"[{self.miner_id}] _process_pending_miner_requests called")
        logger.info(
            f"[{self.miner_id}] pool_init_data: extranonce1={self.pool_init_data['extranonce1']}, extranonce2_size={self.pool_init_data['extranonce2_size']}"
        )

        queued_messages = self._initial_pending_requests
        if queued_messages:
            logger.debug(
                f"[{self.miner_id}] Processing {len(queued_messages)} queued miner requests"
            )
            for msg in queued_messages:
                logger.debug(f"[{self.miner_id}] Queued message: {msg}")
        else:
            logger.warning(f"[{self.miner_id}] No queued messages found!")

        for message in queued_messages:
            try:
                logger.debug(
                    f"[{self.miner_id}] Processing queued message: {message.get('method')}"
                )
                await self._process_miner_message(message)
            except Exception as e:
                logger.error(
                    f"[{self.miner_id}] Error processing queued message: {e}",
                    exc_info=True,
                )

        self._initial_pending_requests = []

    async def _handle_miner_messages(self):
        """
        Main loop reading miner messages.

        Decodes Stratum messages and routes to _process_miner_message
        for state validation and handling.
        """
        try:
            while True:
                line = await self.miner_reader.readline()
                if not line:
                    break

                message_str = line.decode().strip()
                if not message_str:
                    continue

                try:
                    message = json.loads(message_str)
                    await self._process_miner_message(message)
                except json.JSONDecodeError as e:
                    logger.warning(f"[{self.miner_id}] Invalid JSON from miner: {e}")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{self.miner_id}] Error handling miner messages: {e}")
            await self.state_machine.handle_error(e, "miner_handler")

    async def _process_miner_message(self, message: dict[str, Any]):
        """
        Route miner message to appropriate handler.

        Args:
            message: Stratum message dict with method/id/params

        Validates state before processing and queues/rejects invalid messages.
        """
        method = message.get("method")
        msg_id = message.get("id")
        handlers = {
            "mining.subscribe": self._handle_subscribe,
            "mining.authorize": self._handle_authorize,
            "mining.submit": self._handle_submit,
            "mining.extranonce.subscribe": self._handle_extranonce_subscribe,
            "mining.configure": self._handle_configure,
            "mining.suggest_difficulty": self._handle_suggest_difficulty,
            "mining.suggest_target": self._handle_suggest_difficulty,
        }

        if not method:
            # Response to a pool message - forwarding
            await self._send_to_pool(message)
            return

        log_stratum_message(logger, message, prefix=f"[{self.miner_id}] From miner")
        
        # Support for ASIC boost miners
        if method in ["mining.extranonce.subscribe"]:
            handler = handlers.get(method)
            if handler:
                await handler(message, msg_id)
            return

        # Transition enforcement
        if not await self.state_machine.can_handle_message(method):
            if self.state_machine.state in {
                MinerState.CONNECTED,
                MinerState.SUBSCRIBING,
                MinerState.SUBSCRIBED,
            }:
                # Early in handshake - queue for later
                await self.state_machine.queue_message(message)
                logger.debug(
                    f"[{self.miner_id}] Queued {method} in state {self.state_machine.state.name}"
                )
            else:
                # Invalid message for state
                logger.warning(
                    f"[{self.miner_id}] Rejected {method} in state {self.state_machine.state.name}"
                )
                if msg_id is not None:
                    await self._send_error_to_miner(
                        msg_id, "Invalid message for current state"
                    )
            return

        handler = handlers.get(method)
        if handler:
            await handler(message, msg_id)
        else:
            # Unknown method - forward to pool
            logger.debug(
                f"[{self.miner_id}] Forwarding unknown method {method} to pool"
            )
            await self._send_to_pool(message)

    async def _handle_subscribe(self, message: dict[str, Any], msg_id: Any):
        """
        Handle mining.subscribe - returns pool's extranonce data.

        Args:
            message: Subscribe request with optional miner version
            msg_id: Request ID for response

        Returns extranonce1 and extranonce2_size from pool.
        """
        logger.debug(f"[{self.miner_id}] Processing mining.subscribe")

        if not await self.state_machine.transition_to(MinerState.SUBSCRIBING):
            await self._send_error_to_miner(msg_id, "Invalid state for subscribe")
            return

        if (
            self.pool_init_data["extranonce1"] is not None
            and self.pool_init_data["extranonce2_size"]
            and self.pool_init_data["subscription_ids"]
        ):
            response = {
                "id": msg_id,
                "result": [
                    self.pool_init_data["subscription_ids"],
                    self.pool_init_data["extranonce1"],
                    self.pool_init_data["extranonce2_size"],
                ],
                "error": None,
            }
            await self._send_to_miner(response)

            await self.state_machine.transition_to(MinerState.SUBSCRIBED)
        else:
            logger.error(f"[{self.miner_id}] No pool extranonce data available")
            await self._send_error_to_miner(msg_id, "Pool connection not ready")
            await self.state_machine.handle_error(
                Exception("No extranonce"), "subscribe"
            )

    async def _handle_authorize(self, message: dict[str, Any], msg_id: Any):
        """
        Handle mining.authorize - authenticate worker.

        Args:
            message: Auth request with username/password params
            msg_id: Request ID for response

        Extracts min difficulty from password field (d=X format).
        Always returns success, then sends initial work.
        """
        params = message.get("params", [])
        username = params[0] if len(params) >= 1 else ""
        password = params[1] if len(params) >= 2 else ""

        logger.debug(f"[{self.miner_id}] Processing mining.authorize for {username}")

        if not await self.state_machine.transition_to(MinerState.AUTHORIZING):
            await self._send_error_to_miner(msg_id, "Invalid state for authorize")
            return

        _, min_diff = parse_min_difficulty(password)
        if min_diff is not None:
            self.min_difficulty = min_diff
            logger.info(
                f"[{self.miner_id}] Set min_difficulty={min_diff} from password"
            )

        self.stats.worker_name = username
        await self._send_to_miner({"id": msg_id, "result": True, "error": None})
        await self.state_machine.transition_to(MinerState.AUTHORIZED)
        await self._send_initial_work()

    async def _send_initial_work(self):
        """
        Send initial difficulty and job after successful authorization.

        Uses effective difficulty (max of pool/miner requirements) and
        forwards any cached initial job from pool.
        """
        pool_diff = self.pool_init_data["initial_difficulty"] or 1024
        effective_diff = pool_diff
        if self.min_difficulty is not None:
            effective_diff = max(pool_diff, self.min_difficulty)
            if pool_diff > self.min_difficulty:
                logger.info(
                    f"[{self.miner_id}] Initial: Pool diff {pool_diff} > min diff {self.min_difficulty}, using pool diff"
                )
            else:
                logger.info(
                    f"[{self.miner_id}] Initial: Pool diff {pool_diff} < min diff {self.min_difficulty}, using min diff"
                )

        self.stats.update_difficulty(effective_diff)
        await self._send_to_miner(
            {"id": None, "method": "mining.set_difficulty", "params": [effective_diff]}
        )

        if self.pool_init_data["initial_job"]:
            await self._send_to_miner(self.pool_init_data["initial_job"])
            await self.state_machine.transition_to(MinerState.ACTIVE)
        else:
            logger.debug(f"[{self.miner_id}] Waiting for initial job from pool")

    async def _handle_submit(self, message: dict[str, Any], msg_id: Any):
        """
        Handle share submission from miner.

        Args:
            message: Submit with [worker, job_id, extranonce2, ntime, nonce, version_bits]
            msg_id: Request ID for tracking response

        Calculates actual share difficulty and forwards to pool.
        Tracks submission for database storage on pool response.
        """
        # Must be in Active state
        if self.state_machine.state != MinerState.ACTIVE:
            logger.warning(f"[{self.miner_id}] Submit rejected - not in ACTIVE state")
            await self._send_error_to_miner(msg_id, "Not ready for submissions")
            return

        self.pending_calls[msg_id] = "submit"

        params = message.get("params", [])
        worker_name = params[0] if len(params) > 0 else "unknown"
        job_id = params[1] if len(params) > 1 else "unknown"
        extranonce2 = params[2] if len(params) > 2 else ""
        ntime = params[3] if len(params) > 3 else ""
        nonce = params[4] if len(params) > 4 else ""
        version = params[5] if len(params) > 5 else None
        logger.info(f"[{self.miner_id}] {worker_name} - Share submission for job {job_id}")

        # Get job data
        job_data = self.jobs.get_job(job_id)
        if not job_data:
            logger.error(f"[{self.miner_id}] Unknown job {job_id}")
            await self._send_to_miner(
                {"id": msg_id, "result": False, "error": [21, "Job not found", None]}
            )
            return

        extranonce1 = self.pool_session.extranonce1 if self.pool_session else None

        # Calculate actual difficulty
        actual_difficulty = 0.0
        block_hash = ""

        if job_data and extranonce2 and ntime and nonce:
            try:
                actual_difficulty, block_hash = calculate_share_difficulty(
                    job=job_data,
                    extranonce1=extranonce1,
                    extranonce2=extranonce2,
                    ntime=ntime,
                    nonce=nonce,
                    version=version,
                )

                if actual_difficulty > 0:
                    # Store submission details
                    self.pending_calls[msg_id] = {
                        "method": "submit",
                        "worker": worker_name,
                        "job_id": job_id,
                        "extranonce2": extranonce2,
                        "ntime": ntime,
                        "nonce": nonce,
                        "version": version or job_data.get("version", ""),
                        "block_hash": block_hash,
                        "actual_difficulty": actual_difficulty,
                        "pool_difficulty": self.stats.difficulty,
                        "pool_requested_difficulty": self.pool_difficulty,
                    }
                    logger.debug(
                        f"[{self.miner_id}] Calculated difficulty={actual_difficulty:.2f}"
                    )
            except Exception as e:
                logger.error(f"[{self.miner_id}] Difficulty calculation error: {e}")

        # Forward to pool
        message["params"][0] = self.pool_user
        await self._send_to_pool(message)

    async def _handle_pool_messages(self):
        """
        Main loop reading pool messages.

        Decodes Stratum messages and routes to _process_pool_message
        for handling responses and notifications.
        """
        try:
            while True:
                if not self.pool_session:
                    break

                line = await self.pool_session.reader.readline()
                if not line:
                    break

                message_str = line.decode().strip()
                if not message_str:
                    continue

                try:
                    message = json.loads(message_str)
                    await self._process_pool_message(message)
                except json.JSONDecodeError as e:
                    logger.warning(f"[{self.miner_id}] Invalid JSON from pool: {e}")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{self.miner_id}] Error handling pool messages: {e}")
            await self.state_machine.handle_error(e, "pool_handler")

    async def _process_pool_message(self, message: dict[str, Any]):
        """
        Route pool message to appropriate handler.

        Args:
            message: Stratum message from pool

        Handles responses to our requests (submits) and pool
        notifications (jobs, difficulty changes).
        """
        method = message.get("method")
        msg_id = message.get("id")

        log_stratum_message(logger, message, prefix=f"[{self.miner_id}] From pool")

        if msg_id is not None and msg_id in self.pending_calls:
            await self._handle_pool_response(message, msg_id)
            return

        # Handle pool notifications
        if method == "mining.notify":
            await self._handle_job_notify(message)
        elif method == "mining.set_difficulty":
            await self._handle_set_difficulty(message)
        elif method == "mining.set_extranonce":
            await self._handle_set_extranonce(message)
        else:
            if self.state_machine.state in {MinerState.AUTHORIZED, MinerState.ACTIVE}:
                await self._send_to_miner(message)
            else:
                await self.state_machine.queue_message(message)

    async def _handle_job_notify(self, message: dict[str, Any]):
        """Handle job notification from pool."""
        await self._store_job_from_notify(message)

        if self.state_machine.state == MinerState.AUTHORIZED:
            await self.state_machine.transition_to(MinerState.ACTIVE)

        if self.state_machine.state == MinerState.ACTIVE:
            await self._send_to_miner(message)

    async def _store_job_from_notify(self, message: dict[str, Any]):
        """Store job data from mining.notify."""
        params = message.get("params", [])
        if len(params) < 9:
            return

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

        # Add to job queue
        self.jobs.add_job(job_id, job_data)

        logger.debug(f"[{self.miner_id}] Stored job {job_id}")

    async def _handle_set_difficulty(self, message: dict[str, Any]):
        """Handle difficulty change from pool."""
        params = message.get("params", [])
        if not params:
            return

        try:
            pool_diff = float(params[0])
        except (ValueError, TypeError):
            return

        self.pool_difficulty = pool_diff
        self.stats.pool_difficulty = pool_diff

        effective_diff = pool_diff
        if self.min_difficulty is not None:
            effective_diff = max(pool_diff, self.min_difficulty)
            if pool_diff > self.min_difficulty:
                logger.info(
                    f"[{self.miner_id}] Pool diff {pool_diff} > min diff {self.min_difficulty}, using pool diff"
                )
            else:
                logger.info(
                    f"[{self.miner_id}] Pool diff {pool_diff} < min diff {self.min_difficulty}, using min diff"
                )

        if effective_diff != self.stats.difficulty:
            logger.info(
                f"[{self.miner_id}] Difficulty: pool={pool_diff}, "
                f"effective={effective_diff}"
            )
            self.stats.update_difficulty(effective_diff)
            message["params"][0] = effective_diff

            if self.state_machine.state == MinerState.ACTIVE:
                await self._send_to_miner(message)

    async def _handle_set_extranonce(self, message: dict[str, Any]):
        """Handle extranonce change from pool."""
        params = message.get("params", [])
        if len(params) >= 2:
            new_extranonce1 = params[0]
            new_extranonce2_size = params[1]

            logger.info(
                f"[{self.miner_id}] Pool changed extranonce: "
                f"{new_extranonce1} (size {new_extranonce2_size})"
            )

            # Update pool session data
            if self.pool_session:
                self.pool_session.extranonce1 = new_extranonce1
                self.pool_session.extranonce2_size = new_extranonce2_size

            # Update init data for future reconnections
            self.pool_init_data["extranonce1"] = new_extranonce1
            self.pool_init_data["extranonce2_size"] = new_extranonce2_size

            if self.state_machine.state == MinerState.ACTIVE:
                await self._send_to_miner(message)

    async def _handle_pool_response(self, message: dict[str, Any], msg_id: int):
        """
        Process pool's response to our submit request.

        Args:
            message: Pool response with result/error
            msg_id: ID matching our original request

        Stores share data to database and updates stats based on
        accept/reject status.
        """
        pending_data = self.pending_calls.pop(msg_id)

        if isinstance(pending_data, dict) and pending_data.get("method") == "submit":
            result = message.get("result")
            error = message.get("error")
            reject_reason = message.get("reject-reason")
            
            if error is None and reject_reason:
                error = [23, reject_reason]  # 23 (low difficulty)
            
            accepted = result is True and error is None and reject_reason is None

            # Insert receipt to ClickHouse
            if accepted and self.db:
                try:
                    await self.db.insert_share(
                        miner=self.miner_id,
                        worker=pending_data["worker"],
                        pool=f"{self.pool_host}:{self.pool_port}",
                        pool_difficulty=pending_data["pool_difficulty"],
                        actual_difficulty=pending_data["actual_difficulty"],
                        block_hash=pending_data.get("block_hash", ""),
                        pool_requested_difficulty=pending_data.get("pool_requested_difficulty"),
                        pool_label=self.pool_label,
                    )
                except Exception as e:
                    logger.error(f"[{self.miner_id}] Failed to insert share: {e}")
            

            self.stats.record_share(
                accepted=accepted,
                difficulty=pending_data["pool_difficulty"],
                share_difficulty=pending_data["actual_difficulty"],
                pool=f"{self.pool_host}:{self.pool_port}",
                error=json.dumps(error) if error else None,
            )

            # Poor acceptance rate
            total_shares = self.stats.accepted + self.stats.rejected
            if total_shares > 0 and total_shares % 250 == 0:
                acceptance_rate = (self.stats.accepted / total_shares) * 100
                
                if acceptance_rate < 30:
                    logger.warning(
                        f"[{self.miner_id}] {pending_data.get('worker', 'unknown')} - "
                        f"Disconnecting: {acceptance_rate:.1f}% acceptance rate "
                        f"({self.stats.accepted}/{total_shares} accepted)"
                    )
                    
                    await self._send_to_miner(message)
                    self.miner_writer.close()
                    return

            self.stats.last_share_difficulty = pending_data["actual_difficulty"]
            worker_name = pending_data.get("worker", "unknown")
            
            if accepted:
                logger.info(
                    f"[{self.miner_id}] {worker_name} - Share accepted: "
                    f"diff={pending_data['actual_difficulty']:.2f}"
                )
            else:
                reason = "unknown"
                if error:
                    if isinstance(error, list) and len(error) > 1:
                        reason = error[1]
                elif reject_reason:
                    reason = reject_reason
                logger.info(
                    f"[{self.miner_id}] {worker_name} - Share rejected ({reason}): "
                    f"diff={pending_data['actual_difficulty']:.2f}"
                )

        await self._send_to_miner(message)

    async def _handle_extranonce_subscribe(self, message: dict[str, Any], msg_id: Any):
        """Handle extranonce subscription."""
        logger.debug(f"[{self.miner_id}] Acknowledging extranonce subscription")
        await self._send_to_miner({"id": msg_id, "result": True, "error": None})

    async def _handle_configure(self, message: dict[str, Any], msg_id: Any):
        """Handle mining.configure (version rolling)."""
        if self.pool_session:
            if self.pool_session.configure_response is not None:
                await self._send_to_miner({
                    "id": msg_id,
                    "result": self.pool_session.configure_response,
                    "error": None,
                })
                logger.debug(
                    f"[{self.miner_id}] Returned cached configure response to miner"
                )
            else:
                logger.debug(
                    f"[{self.miner_id}] Forwarding late mining.configure to pool"
                )
                await self._send_to_pool(message)
            return

        self.pending_configure = message
        logger.debug(
            f"[{self.miner_id}] Stored configure request until pool connection ready"
        )

    async def _handle_suggest_difficulty(self, message: dict[str, Any], msg_id: Any):
        """Handle difficulty suggestion from miner."""
        await self._send_to_miner({"id": msg_id, "result": True, "error": None})

        params = message.get("params", [])
        if params and len(params) > 0:
            try:
                suggested = float(params[0])
                if suggested > 0 and self.min_difficulty is not None:
                    # Enforce minimum
                    effective = self.min_difficulty
                    self.stats.update_difficulty(effective)
                    await self._send_to_miner(
                        {
                            "id": None,
                            "method": "mining.set_difficulty",
                            "params": [effective],
                        }
                    )

                    message["params"][0] = effective
                if self.pool_session:
                    await self._send_to_pool(message)
                else:
                    # Pool session to be established
                    self._initial_pending_requests.append(message)
            except (ValueError, TypeError):
                # Miner sends invalid msg (saw it sometimes)
                if self.pool_session:
                    await self._send_to_pool(message)
                else:
                    self._initial_pending_requests.append(message)
        else:
            # Miner sends an empty message
            if self.pool_session:
                await self._send_to_pool(message)
            else:
                self._initial_pending_requests.append(message)

    async def _send_to_miner(self, message: dict[str, Any]):
        """Send message to miner."""
        log_stratum_message(logger, message, prefix=f"[{self.miner_id}] To miner")
        data = (json.dumps(message) + "\n").encode()
        self.miner_writer.write(data)
        await self.miner_writer.drain()

    async def _send_to_pool(self, message: dict[str, Any]):
        """Send message to pool."""
        if not self.pool_session:
            logger.error(f"[{self.miner_id}] No pool session for sending")
            return

        log_stratum_message(logger, message, prefix=f"[{self.miner_id}] To pool")
        data = (json.dumps(message) + "\n").encode()
        self.pool_session.writer.write(data)
        await self.pool_session.writer.drain()

    async def _send_error_to_miner(self, msg_id: Any, error_message: str):
        """Send error response to miner."""
        await self._send_to_miner(
            {"id": msg_id, "result": None, "error": [20, error_message, None]}
        )

    def _on_state_change(self, old_state: MinerState, new_state: MinerState):
        """Callback for state changes."""
        worker_name = self.stats.worker_name or "auto"
        worker_prefix = f"{worker_name} - " if worker_name else ""
        
        logger.info(
            f"[{self.miner_id}] {worker_prefix}State change: {old_state.name} -> {new_state.name}"
        )

        if new_state == MinerState.ACTIVE:
            logger.info(f"[{self.miner_id}] {worker_prefix}Miner is now actively mining")
        elif new_state == MinerState.ERROR:
            logger.warning(f"[{self.miner_id}] {worker_prefix}Miner entered error state")

    async def _cleanup(self):
        """
        Clean up session resources on disconnect.

        Transitions state machine to DISCONNECTED and closes
        both miner and pool connections.
        """
        logger.info(f"[{self.miner_id}] Cleaning up session")

        await self.state_machine.start_disconnect()

        try:
            self.miner_writer.close()
            await self.miner_writer.wait_closed()
        except Exception:
            pass

        if self.pool_session:
            try:
                self.pool_session.writer.close()
                await self.pool_session.writer.wait_closed()
            except Exception:
                pass

        await self.state_machine.transition_to(MinerState.DISCONNECTED)

        summary = self.state_machine.get_state_summary()
        logger.info(f"[{self.miner_id}] Final state: {summary}")
