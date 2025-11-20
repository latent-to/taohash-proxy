"""
Mining proxy session handler that bridges miners to upstream pools.

This module manages individual miner connections, handling the Stratum protocol
handshake (subscribe/authorize), job distribution, share submission with difficulty
calculation, and state transitions.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional, Any, Dict

from ..utils.logger import get_logger, log_stratum_message
from ..protocol.difficulty import calculate_share_difficulty, parse_min_difficulty
from ..monitoring.stats import StatsManager
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

        # Pool connection
        self.pool_reader: Optional[asyncio.StreamReader] = None
        self.pool_writer: Optional[asyncio.StreamWriter] = None

        # Pool config
        self.pool_host = pool_host
        self.pool_port = pool_port
        self.pool_user = pool_user
        self.pool_pass = pool_pass
        self.pool_label = pool_label

        # Miner identification
        self.peer = miner_writer.get_extra_info("peername")
        self.miner_id = f"{self.peer[0]}:{self.peer[1]}" if self.peer else "unknown"

        # Stats tracking
        self.stats = stats_manager.register_miner(self.peer, pool_label)
        self.worker_name: Optional[str] = None

        # Difficulty management
        self.min_difficulty: Optional[int] = None
        self.pool_difficulty: Optional[float] = None

        # Protocol tracking
        self.extranonce1: Optional[str] = None
        self.extranonce2_size: Optional[int] = None
        self.subscribe_id: Optional[int] = None
        self.pending_submits: Dict[int, dict] = {}

        # Job tracking
        self.jobs = JobQueue(max_size=25)
        self.db = None

        logger.info(f"[{self.miner_id}] Proxy session initialized")

    async def run(self):
        """
        Main session loop - manages miner pool connection lifecycle.

        Connects to pool immediately and runs two forwarding loops until
        either connection drops.
        """
        try:
            logger.info(
                f"[{self.miner_id}] Connecting to pool {self.pool_host}:{self.pool_port}"
            )
            self.pool_reader, self.pool_writer = await asyncio.open_connection(
                self.pool_host, self.pool_port
            )
            logger.info(f"[{self.miner_id}] Pool connection established")

            miner_to_pool = asyncio.create_task(self._forward_miner_to_pool())
            pool_to_miner = asyncio.create_task(self._forward_pool_to_miner())

            done, pending = await asyncio.wait(
                [miner_to_pool, pool_to_miner], return_when=asyncio.FIRST_COMPLETED
            )

            for task in done:
                try:
                    if task.exception():
                        logger.error(
                            f"[{self.miner_id}] Task failed: {task.exception()}"
                        )
                except asyncio.CancelledError:
                    pass

            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except Exception as e:
            logger.error(f"[{self.miner_id}] Session error: {e}")
        finally:
            await self._cleanup()

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
                            logger.info(f"[{self.miner_id}] Worker: {self.worker_name}")

                        message["params"] = [self.pool_user, self.pool_pass]
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
                            params[0] = self.pool_user

                    elif method == "mining.suggest_difficulty":
                        params = message.get("params", [])
                        if params:
                            logger.info(
                                f"[{self.miner_id}] Miner suggested difficulty: {params[0]}"
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
                                f"[{self.miner_id}] Got extranonce1={self.extranonce1}, "
                                f"extranonce2_size={self.extranonce2_size}"
                            )

                    elif method == "mining.notify":
                        params = message.get("params", [])
                        if len(params) >= 9:
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
            pool=f"{self.pool_host}:{self.pool_port}",
            error=json.dumps(error) if error else None,
        )

        if accepted and self.db and actual_difficulty > 0:
            try:
                await self.db.insert_share(
                    miner=self.miner_id,
                    worker=submit_data["worker"],
                    pool=f"{self.pool_host}:{self.pool_port}",
                    pool_difficulty=self.stats.difficulty,
                    actual_difficulty=actual_difficulty,
                    block_hash=block_hash if "block_hash" in locals() else "",
                    pool_requested_difficulty=self.pool_difficulty,
                    pool_label=self.pool_label,
                    share_timestamp=submit_data.get("received_at"),
                )
            except Exception as e:
                logger.error(f"[{self.miner_id}] Failed to insert share: {e}")

        if accepted:
            logger.info(
                f"[{self.miner_id}] {submit_data['worker']} - Share accepted: "
                f"diff={actual_difficulty:.2f}"
            )
        else:
            reason = "unknown"
            if error and isinstance(error, list) and len(error) > 1:
                reason = error[1]
            elif reject_reason:
                reason = reject_reason
            logger.info(
                f"[{self.miner_id}] {submit_data['worker']} - Share rejected ({reason}): "
                f"diff={actual_difficulty:.2f}"
            )

        # Check acceptance ratio every 250 shares
        total_shares = self.stats.accepted + self.stats.rejected
        if total_shares > 0 and total_shares % 250 == 0:
            acceptance_rate = (self.stats.accepted / total_shares) * 100

            if acceptance_rate < 30:
                logger.warning(
                    f"[{self.miner_id}] {submit_data.get('worker', 'unknown')} - "
                    f"Disconnecting: {acceptance_rate:.1f}% acceptance rate "
                    f"({self.stats.accepted}/{total_shares} accepted)"
                )

                # Close the miner connection
                self.miner_writer.close()
                await self.miner_writer.wait_closed()

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
