import os
from datetime import datetime, timezone
from typing import Optional

import clickhouse_connect
import urllib3

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

    async def init(self):
        """Initialize connection to ClickHouse."""
        logger.info(f"Initializing ClickHouse connection to {self.host}:{self.port}")

        try:
            pool_mgr = urllib3.PoolManager(maxsize=200)
            self.client = await clickhouse_connect.get_async_client(
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                database="default",
                secure=False,
                compress=True,
                pool_mgr=pool_mgr,
                settings={
                    "async_insert": 1,
                    "wait_for_async_insert": 0,
                    "async_insert_busy_timeout_min_ms": 5_000,
                    "async_insert_max_data_size": 1048576,
                    "async_insert_busy_timeout_max_ms": 15_000,
                },
            )

            result = await self.client.query("SELECT version()")
            version = result.result_rows[0][0]
            logger.info(f"Connected to ClickHouse server version: {version}")

            await self._migrate_schema()
            await self._create_tables()

            return True
        except Exception as e:
            logger.error(f"Failed to connect to ClickHouse: {e}")
            if self.required:
                raise
            return False

    async def _migrate_schema(self):
        """Run schema migrations for existing tables."""
        logger.info("Running schema migrations...")

        await self.client.command("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version Int32,
                description String,
                applied_at DateTime DEFAULT now()
            ) ENGINE = MergeTree()
            ORDER BY version
        """)

        result = await self.client.query("SELECT version FROM schema_migrations")
        applied_versions = {row[0] for row in result.result_rows}

        migrations = [
            (
                1,
                "ALTER TABLE shares MODIFY COLUMN pool LowCardinality(String)",
                "Modify pool column to LowCardinality",
            ),
            (
                2,
                "ALTER TABLE shares MODIFY COLUMN pool_label LowCardinality(String) DEFAULT 'unknown'",
                "Modify pool_label column to LowCardinality",
            ),
            (3, "DROP VIEW IF EXISTS worker_stats_5m", "Drop worker_stats_5m view"),
            (4, "DROP VIEW IF EXISTS worker_stats_60m", "Drop worker_stats_60m view"),
            (5, "DROP VIEW IF EXISTS worker_stats_24h", "Drop worker_stats_24h view"),
            (6, "DROP VIEW IF EXISTS pool_stats_5m", "Drop pool_stats_5m view"),
            (7, "DROP VIEW IF EXISTS pool_stats_60m", "Drop pool_stats_60m view"),
            (8, "DROP VIEW IF EXISTS pool_stats_24h", "Drop pool_stats_24h view"),
            (9, "DROP VIEW IF EXISTS worker_state", "Drop worker_state view"),
            (
                10,
                "DROP VIEW IF EXISTS worker_stats_mv",
                "Drop worker_stats_mv materialized view",
            ),
            (
                11,
                "DROP VIEW IF EXISTS pool_stats_mv",
                "Drop pool_stats_mv materialized view",
            ),
            (
                12,
                "ALTER TABLE daily_rewards ADD COLUMN paid Boolean DEFAULT false",
                "Add paid column to daily_rewards table",
            ),
            (
                13,
                """ALTER TABLE daily_rewards 
                   UPDATE paid = true 
                   WHERE date IN (
                       '2025-07-21',
                       '2025-07-22',
                       '2025-07-23',
                       '2025-07-24',
                       '2025-07-25',
                       '2025-07-26',
                       '2025-07-27',
                       '2025-07-28'
                   )""",
                "Backfill paid=true for known paid dates July 21-28",
            ),
            (
                14,
                "ALTER TABLE daily_rewards ADD COLUMN payment_proof_url String DEFAULT ''",
                "Add payment_proof_url field for storing payment documentation links",
            ),
            (
                15,
                "ALTER TABLE tides_rewards ADD COLUMN source_type LowCardinality(String) DEFAULT 'pool_payout'",
                "Add source_type column to distinguish coinbase vs pool_payout rewards",
            ),
        ]

        for version, migration_sql, description in migrations:
            if version not in applied_versions:
                try:
                    await self.client.command(migration_sql)
                    await self.client.command(
                        """INSERT INTO schema_migrations (version, description) 
                           VALUES (%(version)s, %(description)s)""",
                        parameters={"version": version, "description": description},
                    )
                    logger.info(f"Migration {version} applied: {description}")
                except Exception as e:
                    logger.error(
                        f"Migration {version} failed: {description} - {str(e)}"
                    )
            else:
                logger.debug(f"Migration {version} already applied: {description}")

    def _get_schema_statements(self):
        """Return schema statements as a list of SQL strings."""
        return [
            """CREATE TABLE IF NOT EXISTS shares (
                ts DateTime DEFAULT now(),
                miner String,
                worker String,
                pool LowCardinality(String),
                pool_difficulty Float32,
                actual_difficulty Float32,
                block_hash String,
                pool_requested_difficulty Float32 DEFAULT 0,
                pool_label LowCardinality(String) DEFAULT 'unknown',
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
            ORDER BY (worker, ts)
            AS
            SELECT
                toStartOfMinute(ts) as ts,
                worker,
                anyLastState(miner) as latest_miner,
                countState() as share_count,
                sumState(pool_difficulty) as total_pool_difficulty,
                sumState(actual_difficulty) as total_actual_difficulty,
                maxState(actual_difficulty) as max_difficulty
            FROM shares
            GROUP BY ts, worker""",
            # Pool stats materialized view
            """CREATE MATERIALIZED VIEW IF NOT EXISTS pool_stats_mv
            ENGINE = AggregatingMergeTree()
            PARTITION BY toYYYYMM(ts)
            ORDER BY ts
            AS
            SELECT
                toStartOfMinute(ts) as ts,
                uniqState(worker) as unique_workers,
                countState() as total_shares,
                sumState(pool_difficulty) as sum_pool_difficulty,
                sumState(actual_difficulty) as sum_actual_difficulty,
                maxState(actual_difficulty) as max_actual_difficulty
            FROM shares
            GROUP BY ts""",
            # Worker stats views
            """CREATE VIEW IF NOT EXISTS worker_stats_5m AS
            SELECT
                worker,
                anyLastMerge(latest_miner) as latest_miner,
                countMerge(share_count) as shares,
                sumMerge(total_pool_difficulty) as pool_difficulty_sum,
                sumMerge(total_actual_difficulty) as actual_difficulty_sum,
                maxMerge(max_difficulty) as max_difficulty,
                sumMerge(total_pool_difficulty) * 4294967296 / 300 as hashrate
            FROM worker_stats_mv
            WHERE ts > now() - INTERVAL 5 MINUTE
            GROUP BY worker""",
            """CREATE VIEW IF NOT EXISTS worker_stats_60m AS
            SELECT
                worker,
                anyLastMerge(latest_miner) as latest_miner,
                countMerge(share_count) as shares,
                sumMerge(total_pool_difficulty) as pool_difficulty_sum,
                sumMerge(total_actual_difficulty) as actual_difficulty_sum,
                maxMerge(max_difficulty) as max_difficulty,
                sumMerge(total_pool_difficulty) * 4294967296 / 3600 as hashrate
            FROM worker_stats_mv
            WHERE ts > now() - INTERVAL 60 MINUTE
            GROUP BY worker""",
            """CREATE VIEW IF NOT EXISTS worker_stats_24h AS
            SELECT
                worker,
                anyLastMerge(latest_miner) as latest_miner,
                countMerge(share_count) as shares,
                sumMerge(total_pool_difficulty) as pool_difficulty_sum,
                sumMerge(total_actual_difficulty) as actual_difficulty_sum,
                maxMerge(max_difficulty) as max_difficulty,
                sumMerge(total_pool_difficulty) * 4294967296 / 86400 as hashrate
            FROM worker_stats_mv
            WHERE ts > now() - INTERVAL 24 HOUR
            GROUP BY worker""",
            # Pool stats views
            """CREATE VIEW IF NOT EXISTS pool_stats_5m AS
            SELECT
                uniqMerge(unique_workers) as active_workers,
                countMerge(total_shares) as shares,
                sumMerge(sum_pool_difficulty) as pool_difficulty_sum,
                sumMerge(sum_actual_difficulty) as actual_difficulty_sum,
                maxMerge(max_actual_difficulty) as max_difficulty,
                sumMerge(sum_pool_difficulty) * 4294967296 / 300 as hashrate
            FROM pool_stats_mv
            WHERE ts > now() - INTERVAL 5 MINUTE
            """,
            """CREATE VIEW IF NOT EXISTS pool_stats_60m AS
            SELECT
                uniqMerge(unique_workers) as active_workers,
                countMerge(total_shares) as shares,
                sumMerge(sum_pool_difficulty) as pool_difficulty_sum,
                sumMerge(sum_actual_difficulty) as actual_difficulty_sum,
                maxMerge(max_actual_difficulty) as max_difficulty,
                sumMerge(sum_pool_difficulty) * 4294967296 / 3600 as hashrate
            FROM pool_stats_mv
            WHERE ts > now() - INTERVAL 60 MINUTE
            """,
            """CREATE VIEW IF NOT EXISTS pool_stats_24h AS
            SELECT
                uniqMerge(unique_workers) as active_workers,
                countMerge(total_shares) as shares,
                sumMerge(sum_pool_difficulty) as pool_difficulty_sum,
                sumMerge(sum_actual_difficulty) as actual_difficulty_sum,
                maxMerge(max_actual_difficulty) as max_difficulty,
                sumMerge(sum_pool_difficulty) * 4294967296 / 86400 as hashrate
            FROM pool_stats_mv
            WHERE ts > now() - INTERVAL 24 HOUR
            """,
            # Worker's latest contribution
            """CREATE MATERIALIZED VIEW IF NOT EXISTS worker_pool_latest_share_mv
            ENGINE = ReplacingMergeTree()
            ORDER BY worker
            AS
            SELECT
                worker,
                max(ts) as last_share_ts
            FROM shares
            GROUP BY worker""",
            # Daily worker share value - used for rewards
            """CREATE MATERIALIZED VIEW IF NOT EXISTS worker_daily_share_value
            ENGINE = AggregatingMergeTree()
            PARTITION BY toYYYYMM(date)
            ORDER BY (date, worker)
            AS
            SELECT
                toDate(ts) as date,
                worker,
                countState() as shares,
                sumState(actual_difficulty) as share_value,
                sumState(pool_difficulty) as pool_difficulty_sum
            FROM shares
            GROUP BY date, worker""",
            # Daily rewards table
            """CREATE TABLE IF NOT EXISTS daily_rewards (
                date Date,
                amount Float64,
                updated_at DateTime DEFAULT now()
            )
            ENGINE = ReplacingMergeTree(updated_at)
            ORDER BY date""",
            # Difficulty window configuration - single row config table
            """CREATE TABLE IF NOT EXISTS tides_config (
                id UInt8 DEFAULT 1,
                multiplier Float64,
                network_difficulty Float64,
                updated_at DateTime DEFAULT now()
            ) ENGINE = MergeTree()
            ORDER BY id""",
            # Insert default config when creating the config table
            """INSERT INTO tides_config (id, multiplier, network_difficulty, updated_at)
            SELECT 1, 8, 129700000000000, now()
            WHERE NOT EXISTS (SELECT 1 FROM tides_config WHERE id = 1)""",
            # General configuration
            """CREATE TABLE IF NOT EXISTS general_config (
                id UInt8 DEFAULT 1,
                worker_percentage Float64,
                updated_at DateTime DEFAULT now()
            ) ENGINE = MergeTree()
            ORDER BY id""",
            # Insert default general config when creating the table
            """INSERT INTO general_config (id, worker_percentage, updated_at)
            SELECT 1, 0.015, now()
            WHERE NOT EXISTS (SELECT 1 FROM general_config WHERE id = 1)""",
            # TIDES window table
            """CREATE TABLE IF NOT EXISTS tides_window (
                id UInt8 DEFAULT 1,
                share_log_window Float64,
                network_difficulty Float64,
                multiplier Float64,
                window_start DateTime,
                window_end DateTime,
                total_difficulty_in_window Float64,
                total_workers UInt32,
                workers_json String,
                updated_at DateTime DEFAULT now()
            ) ENGINE = MergeTree()
            ORDER BY id""",
            # TIDES rewards table
            """CREATE TABLE IF NOT EXISTS tides_rewards (
                tx_hash String,
                block_height UInt64,
                btc_amount Float64,
                confirmed_at DateTime DEFAULT now(),
                discovered_at DateTime DEFAULT now(),
                tides_window String,
                processed Boolean DEFAULT false,
                updated_at DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(updated_at)
            ORDER BY (block_height, tx_hash)""",
            # User rewards
            # Current User Balances (Dynamically updated)
            """CREATE TABLE IF NOT EXISTS user_rewards (
                worker String,
                unpaid_amount Float64,
                paid_amount Float64,
                total_earned Float64,
                last_updated DateTime,
                updated_by String
            ) ENGINE = ReplacingMergeTree(last_updated)
            ORDER BY worker""",
            # Earnings History (BTC earnings for miner)
            """CREATE TABLE IF NOT EXISTS user_earnings (
                earning_id String,
                worker String,
                btc_amount Float64,
                earning_type LowCardinality(String),
                reference Nullable(String),
                tides_reward_id Nullable(String),
                metadata String,
                earned_at DateTime,
                created_at DateTime
            ) ENGINE = MergeTree()
            PARTITION BY toYYYYMM(earned_at)
            ORDER BY (worker, earned_at, earning_id)""",
            # User Payouts (BTC payouts for miner)
            """CREATE TABLE IF NOT EXISTS user_payouts (
                payout_id String,
                worker String,
                btc_amount Float64,
                payout_batch_id Nullable(String),
                bitcoin_tx_hash String,
                notes String,
                paid_at DateTime,
                created_at DateTime
            ) ENGINE = MergeTree()
            PARTITION BY toYYYYMM(paid_at)
            ORDER BY (worker, paid_at, payout_id)""",
            # Batch payouts records
            """CREATE TABLE IF NOT EXISTS payout_batches (
                batch_id String,
                total_amount Float64,
                user_count Int32,
                payout_data String,
                payment_method LowCardinality(String) DEFAULT 'bitcoin',
                external_reference String,
                notes String,
                processed_at DateTime,
                processed_by String,
                created_at DateTime DEFAULT now()
            ) ENGINE = MergeTree()
            ORDER BY (processed_at, batch_id)""",
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

            # TBD if we wanna do this.
            # Backfill materialized views with last 24 hours of data
            # await self._backfill_materialized_views()

        except Exception as e:
            logger.error(f"Failed to create tables: {e}")

    async def _backfill_materialized_views(self):
        """Backfill materialized views with last 24 hours of data."""
        logger.info("Backfilling materialized views with last 24 hours of data...")

        backfill_queries = [
            # Backfill worker_stats_mv
            """INSERT INTO worker_stats_mv
            SELECT
                toStartOfMinute(ts) as ts,
                worker,
                anyLastState(miner) as latest_miner,
                countState() as share_count,
                sumState(pool_difficulty) as total_pool_difficulty,
                sumState(actual_difficulty) as total_actual_difficulty,
                maxState(actual_difficulty) as max_difficulty
            FROM shares
            WHERE ts >= now() - INTERVAL 24 HOUR
            GROUP BY ts, worker""",
            # Backfill pool_stats_mv
            """INSERT INTO pool_stats_mv
            SELECT
                toStartOfMinute(ts) as ts,
                uniqState(worker) as unique_workers,
                countState() as total_shares,
                sumState(pool_difficulty) as sum_pool_difficulty,
                sumState(actual_difficulty) as sum_actual_difficulty,
                maxState(actual_difficulty) as max_actual_difficulty
            FROM shares
            WHERE ts >= now() - INTERVAL 24 HOUR
            GROUP BY ts""",
            # Backfill worker_pool_latest_share_mv
            """INSERT INTO worker_pool_latest_share_mv
            SELECT
                worker,
                max(ts) as last_share_ts
            FROM shares
            WHERE ts >= now() - INTERVAL 24 HOUR
            GROUP BY worker""",
        ]

        for query in backfill_queries:
            try:
                await self.client.command(query)
                logger.info(f"Backfill successful: {query[:20]}...")
            except Exception as e:
                logger.error(f"Backfill failed: {str(e)}")

    async def close(self):
        """Close ClickHouse client."""
        if self.client:
            await self.client.close()
            logger.info("ClickHouse connection closed")

    async def insert_share(
        self,
        miner: str,
        worker: str,
        pool: str,
        pool_difficulty: float,
        actual_difficulty: float,
        block_hash: str = "",
        share_timestamp: Optional[datetime] = None,
        **kwargs,
    ) -> None:
        """
        Insert a share submission directly to ClickHouse.

        Args:
            miner: Miner identifier
            worker: Worker name
            pool: Pool address
            pool_difficulty: Pool difficulty
            actual_difficulty: Actual share difficulty
            block_hash: Block hash (will be reversed before storage)
            share_timestamp: Timestamp when share was received
        """
        if not self.client:
            logger.error("ClickHouse client is not initialised")
            return

        ts = share_timestamp or datetime.now(timezone.utc)
        try:
            await self.client.insert(
                "shares",
                [
                    [
                        ts,
                        miner,
                        worker,
                        pool,
                        pool_difficulty,
                        actual_difficulty,
                        block_hash[::-1] if block_hash else "",
                        kwargs.get("pool_requested_difficulty", 0.0),
                        kwargs.get("pool_label", "unknown"),
                    ]
                ],
                column_names=[
                    "ts",
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
        except Exception as e:
            logger.error(f"Insert failed: {e}")
            if self.required:
                raise
