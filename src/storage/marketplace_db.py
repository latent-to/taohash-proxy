"""
PostgreSQL connection pool and schema for marketplace orders.

Provides:
- Connection pool management with asyncpg
- Schema creation and migrations
"""

import os
from typing import Optional

import asyncpg

from ..utils.logger import get_logger

logger = get_logger(__name__)


class MarketplaceDB:
    """PostgreSQL connection pool and schema manager for marketplace."""

    def __init__(self):
        """Initialize with connection parameters from environment variables."""
        self.pool: Optional[asyncpg.Pool] = None
        self.host = os.environ.get("POSTGRES_HOST", "localhost")
        self.port = int(os.environ.get("POSTGRES_PORT", "5432"))
        self.user = os.environ.get("POSTGRES_USER", "taohash")
        self.password = os.environ.get("POSTGRES_PASSWORD", "taohash123")
        self.database = os.environ.get("POSTGRES_DB", "marketplace")
        self.required = os.environ.get("POSTGRES_REQUIRED", "false").lower() == "true"

    async def init(self) -> bool:
        """
        Initialize connection pool and create tables.

        Returns:
            True if connected successfully, False otherwise
        """
        logger.info(f"Initializing PostgreSQL connection to {self.host}:{self.port}")

        try:
            self.pool = await asyncpg.create_pool(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
                min_size=5,
                max_size=20,
            )

            # Test connection
            async with self.pool.acquire() as conn:
                version = await conn.fetchval("SELECT version()")
                logger.info(f"Connected to PostgreSQL: {version.split(',')[0]}")

            await self._create_tables()
            return True

        except Exception as e:
            logger.error(f"Failed to connect to PostgreSQL: {e}")
            if self.required:
                raise
            return False

    async def close(self):
        """Close connection pool."""
        if self.pool:
            await self.pool.close()
            logger.info("PostgreSQL connection pool closed")

    async def _create_tables(self):
        """Create marketplace tables if they don't exist."""
        async with self.pool.acquire() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS marketplace_orders (
                    order_id TEXT PRIMARY KEY,
                    host TEXT NOT NULL,
                    port INT NOT NULL,
                    username TEXT NOT NULL,
                    password TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active','paused','fulfilled','cancelled')) DEFAULT 'active',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    cancelled_at TIMESTAMPTZ,

                    price_btc_eh_day DECIMAL(28,20) NOT NULL,
                    amount_btc DECIMAL(28,20) NOT NULL,
                    balance_btc DECIMAL(28,20) NOT NULL,
                    limit_eh_s DECIMAL(28,20),

                    fulfilled_at TIMESTAMPTZ,
                    paused_at TIMESTAMPTZ,

                    total_shares_credited BIGINT DEFAULT 0,
                    total_share_value_credited DECIMAL(28,20) DEFAULT 0,
                    last_share_credited_at TIMESTAMPTZ
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS miner_assignments (
                    assignment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    worker_ip TEXT NOT NULL,
                    order_id TEXT NOT NULL REFERENCES marketplace_orders(order_id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK (status IN ('active','completed','cancelled')) DEFAULT 'active',
                    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    unassigned_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            # Filtering orders by status and balance for share-based processing
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_status_balance ON marketplace_orders(status, balance_btc)"
            )
            # Partial unique index: only one active assignment per worker
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_assignment_worker ON miner_assignments(worker_ip) WHERE status = 'active'"
            )
            # Composite index for order + status lookups
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_assignments_order_status ON miner_assignments(order_id, status)"
            )
            # Index for worker assignment history queries
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_assignments_worker_assigned ON miner_assignments(worker_ip, assigned_at DESC)"
            )

            logger.info("Marketplace tables created/verified")
