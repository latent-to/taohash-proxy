import asyncio
import os
import time
from typing import Optional

import clickhouse_connect

from ..utils.logger import get_logger

logger = get_logger(__name__)


class StatsDB:
    """
    Database handler for storing accepted shares.
    """

    def __init__(self):
        """Initialize with connection parameters from environment variables."""
        self.client = None
        self.host = os.environ.get("CLICKHOUSE_HOST", "clickhouse")
        self.port = int(os.environ.get("CLICKHOUSE_PORT", "8123"))
        self.username = os.environ.get("CLICKHOUSE_USER", "default")
        self.password = os.environ.get("CLICKHOUSE_PASSWORD", "taohash123")
        self.required = os.environ.get("CLICKHOUSE_REQUIRED", "false").lower() == "true"

        self.BATCH_SIZE = 1000
        self.FLUSH_INTERVAL = 10
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=100_000)

        self._writer_task: Optional[asyncio.Task] = None
        self._stop_event: asyncio.Event = asyncio.Event()

    async def init(self):
        """Initialize connection to ClickHouse."""
        logger.info(f"Initializing ClickHouse connection to {self.host}:{self.port}")

        try:
            self.client = await clickhouse_connect.get_async_client(
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                database="default",
                secure=False,
                compress=True,
            )

            result = await self.client.query("SELECT version()")
            version = result.result_rows[0][0]
            logger.info(f"Connected to ClickHouse server version: {version}")

            await self._create_tables()
            self._writer_task = asyncio.create_task(self._writer_loop())

            return True
        except Exception as e:
            logger.error(f"Failed to connect to ClickHouse: {e}")
            if self.required:
                raise
            return False

    def _get_schema_statements(self):
        """Return schema statements as a list of SQL strings."""
        return [
            """CREATE TABLE IF NOT EXISTS shares (
                ts DateTime DEFAULT now(),
                miner String,
                worker String,
                pool String,
                pool_difficulty Float32,
                actual_difficulty Float32,
                block_hash String,
                pool_requested_difficulty Float32 DEFAULT 0,
                pool_label String DEFAULT 'unknown',
                INDEX idx_worker (worker) TYPE bloom_filter GRANULARITY 1,
                INDEX idx_ts (ts) TYPE minmax GRANULARITY 1
            )
            ENGINE = MergeTree()
            PARTITION BY toYYYYMM(ts)
            ORDER BY (ts, worker)
            TTL ts + INTERVAL 2 DAY TO VOLUME 'cold'
            SETTINGS index_granularity = 8192, storage_policy = 'tiered'""",
            # Worker stats materialized view
            """CREATE MATERIALIZED VIEW IF NOT EXISTS worker_stats_mv
            ENGINE = AggregatingMergeTree()
            PARTITION BY toYYYYMM(ts)
            ORDER BY (worker, pool_label, ts)
            AS
            SELECT
                toStartOfMinute(ts) as ts,
                worker,
                pool_label,
                anyLastState(miner) as latest_miner,
                countState() as share_count,
                sumState(pool_difficulty) as total_pool_difficulty,
                sumState(actual_difficulty) as total_actual_difficulty,
                maxState(actual_difficulty) as max_difficulty
            FROM shares
            GROUP BY ts, worker, pool_label""",
            # Pool stats materialized view
            """CREATE MATERIALIZED VIEW IF NOT EXISTS pool_stats_mv
            ENGINE = AggregatingMergeTree()
            PARTITION BY toYYYYMM(ts)
            ORDER BY (pool_label, ts)
            AS
            SELECT
                toStartOfMinute(ts) as ts,
                pool_label,
                uniqState(worker) as unique_workers,
                countState() as total_shares,
                sumState(pool_difficulty) as sum_pool_difficulty,
                sumState(actual_difficulty) as sum_actual_difficulty,
                maxState(actual_difficulty) as max_actual_difficulty
            FROM shares
            GROUP BY ts, pool_label""",
            # Worker stats views
            """CREATE VIEW IF NOT EXISTS worker_stats_5m AS
            SELECT
                worker,
                pool_label,
                anyLastMerge(latest_miner) as latest_miner,
                countMerge(share_count) as shares,
                sumMerge(total_pool_difficulty) as pool_difficulty_sum,
                sumMerge(total_actual_difficulty) as actual_difficulty_sum,
                maxMerge(max_difficulty) as max_difficulty,
                sumMerge(total_pool_difficulty) * 4294967296 / 300 as hashrate
            FROM worker_stats_mv
            WHERE ts > now() - INTERVAL 5 MINUTE
            GROUP BY worker, pool_label""",
            """CREATE VIEW IF NOT EXISTS worker_stats_60m AS
            SELECT
                worker,
                pool_label,
                anyLastMerge(latest_miner) as latest_miner,
                countMerge(share_count) as shares,
                sumMerge(total_pool_difficulty) as pool_difficulty_sum,
                sumMerge(total_actual_difficulty) as actual_difficulty_sum,
                maxMerge(max_difficulty) as max_difficulty,
                sumMerge(total_pool_difficulty) * 4294967296 / 3600 as hashrate
            FROM worker_stats_mv
            WHERE ts > now() - INTERVAL 60 MINUTE
            GROUP BY worker, pool_label""",
            """CREATE VIEW IF NOT EXISTS worker_stats_24h AS
            SELECT
                worker,
                pool_label,
                anyLastMerge(latest_miner) as latest_miner,
                countMerge(share_count) as shares,
                sumMerge(total_pool_difficulty) as pool_difficulty_sum,
                sumMerge(total_actual_difficulty) as actual_difficulty_sum,
                maxMerge(max_difficulty) as max_difficulty,
                sumMerge(total_pool_difficulty) * 4294967296 / 86400 as hashrate
            FROM worker_stats_mv
            WHERE ts > now() - INTERVAL 24 HOUR
            GROUP BY worker, pool_label""",
            # Pool stats views
            """CREATE VIEW IF NOT EXISTS pool_stats_5m AS
            SELECT
                pool_label,
                uniqMerge(unique_workers) as active_workers,
                countMerge(total_shares) as shares,
                sumMerge(sum_pool_difficulty) as pool_difficulty_sum,
                sumMerge(sum_actual_difficulty) as actual_difficulty_sum,
                maxMerge(max_actual_difficulty) as max_difficulty,
                sumMerge(sum_pool_difficulty) * 4294967296 / 300 as hashrate
            FROM pool_stats_mv
            WHERE ts > now() - INTERVAL 5 MINUTE
            GROUP BY pool_label""",
            """CREATE VIEW IF NOT EXISTS pool_stats_60m AS
            SELECT
                pool_label,
                uniqMerge(unique_workers) as active_workers,
                countMerge(total_shares) as shares,
                sumMerge(sum_pool_difficulty) as pool_difficulty_sum,
                sumMerge(sum_actual_difficulty) as actual_difficulty_sum,
                maxMerge(max_actual_difficulty) as max_difficulty,
                sumMerge(sum_pool_difficulty) * 4294967296 / 3600 as hashrate
            FROM pool_stats_mv
            WHERE ts > now() - INTERVAL 60 MINUTE
            GROUP BY pool_label""",
            """CREATE VIEW IF NOT EXISTS pool_stats_24h AS
            SELECT
                pool_label,
                uniqMerge(unique_workers) as active_workers,
                countMerge(total_shares) as shares,
                sumMerge(sum_pool_difficulty) as pool_difficulty_sum,
                sumMerge(sum_actual_difficulty) as actual_difficulty_sum,
                maxMerge(max_actual_difficulty) as max_difficulty,
                sumMerge(sum_pool_difficulty) * 4294967296 / 86400 as hashrate
            FROM pool_stats_mv
            WHERE ts > now() - INTERVAL 24 HOUR
            GROUP BY pool_label""",
            # Worker state view
            """CREATE VIEW IF NOT EXISTS worker_state AS
            SELECT
                worker,
                argMax(miner, ts) as latest_miner,
                argMax(pool_label, ts) as pool_label,
                max(ts) as last_share_ts,
                CASE 
                    WHEN max(ts) > now() - INTERVAL 10 MINUTE THEN 'ok'
                    ELSE 'offline'
                END as state
            FROM shares
            GROUP BY worker""",
            # Worker's latest contribution
            """CREATE MATERIALIZED VIEW IF NOT EXISTS worker_pool_latest_share_mv
            ENGINE = ReplacingMergeTree()
            ORDER BY (worker, pool_label)
            AS
            SELECT
                worker,
                pool_label,
                max(ts) as last_share_ts
            FROM shares
            GROUP BY worker, pool_label""",
        ]

    async def _create_tables(self):
        """Create all tables and views."""
        try:
            for stmt in self._get_schema_statements():
                try:
                    await self.client.command(stmt)
                except Exception as e:
                    logger.error(f"Failed to create table: {e}")
                    pass

            logger.info("Database schema created/verified")
        except Exception as e:
            logger.error(f"Failed to create tables: {e}")

    async def insert_share(
        self,
        miner: str,
        worker: str,
        pool: str,
        pool_difficulty: float,
        actual_difficulty: float,
        block_hash: str = "",
        **kwargs,
    ) -> None:
        """
        Insert a share submission with minimal data.

        Args:
            miner: Miner identifier
            worker: Worker name
            pool: Pool address
            pool_difficulty: Pool difficulty
            actual_difficulty: Actual share difficulty
            block_hash: Block hash (will be reversed before storage)
        """
        if not self.client:
            return

        row = [
            miner,
            worker,
            pool,
            pool_difficulty,
            actual_difficulty,
            block_hash[::-1] if block_hash else "",  # reversed block hash
            kwargs.get("pool_requested_difficulty", 0.0),
            kwargs.get("pool_label", "unknown"),
        ]
        await self.queue.put(row)

    async def close(self):
        """Flush remaining data, stop writer task, close ClickHouse client."""
        self._stop_event.set()

        if self._writer_task:
            await self._writer_task

        if self.client:
            await self.client.close()
            logger.info("ClickHouse connection closed")

    async def _writer_loop(self) -> None:
        """
        For batching and inserting.
        """
        batch: list[list] = []
        next_flush_at = time.time() + self.FLUSH_INTERVAL
        logger.warning("Writer loop started")

        while not self._stop_event.is_set():
            timeout = max(0, next_flush_at - time.time())
            try:
                row = await asyncio.wait_for(self.queue.get(), timeout)
                batch.append(row)

                # Flush on size limit
                if len(batch) >= self.BATCH_SIZE:
                    await self._flush(batch)
                    batch.clear()
                    next_flush_at = time.time() + self.FLUSH_INTERVAL
            except asyncio.TimeoutError:
                # Flush on interval
                if batch:
                    await self._flush(batch)
                    batch.clear()
                next_flush_at = time.time() + self.FLUSH_INTERVAL

        # Final drain when stop signal received
        while not self.queue.empty():
            batch.append(self.queue.get_nowait())
            if len(batch) >= self.BATCH_SIZE:
                await self._flush(batch)
                batch.clear()
        if batch:
            await self._flush(batch)

    async def _flush(self, rows: list[list]) -> None:
        """
        Flush batched shares to ClickHouse.
        """
        if not self.client:
            logger.error("ClickHouse client is not initialised")
            return
        try:
            await self.client.insert(
                "shares",
                rows,
                column_names=[
                    "miner",
                    "worker",
                    "pool",
                    "pool_difficulty",
                    "actual_difficulty",
                    "block_hash",
                    "pool_requested_difficulty",
                    "pool_label",
                ],
            )
            logger.info("Flushed %d shares to ClickHouse", len(rows))
        except Exception as e:
            logger.error("Batch insert failed: %s", e)
            with open("failed_shares.log", "a") as f:
                f.write(repr(rows) + "\n")
            if self.required:
                raise
