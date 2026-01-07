"""Marketplace order queries and atomic operations."""

from decimal import Decimal
from typing import Optional

import asyncpg

from src.utils.logger import get_logger

logger = get_logger(__name__)


async def create_order(
    pool: asyncpg.Pool,
    order_id: str,
    host: str,
    port: int,
    user: str,
    password: str,
    price_btc_eh_day: Decimal,
    amount_btc: Decimal,
    limit_eh_s: Optional[Decimal] = None,
) -> bool:
    """
    Create a new share-based marketplace order.

    Args:
        pool: asyncpg connection pool
        order_id: Unique order identifier
        host: Pool host address
        port: Pool port
        user: Pool username
        password: Pool password
        price_btc_eh_day: Price in BTC per Exahash per Day
        amount_btc: Total budget in BTC (initial balance)
        limit_eh_s: Optional hashrate speed cap in EH/s

    Returns:
        True if created successfully, False otherwise
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO marketplace_orders (
                    order_id, host, port, username, password,
                    price_btc_eh_day, amount_btc, balance_btc, limit_eh_s, status
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $7, $8, 'active')
                """,
                order_id,
                host,
                port,
                user,
                password,
                price_btc_eh_day,
                amount_btc,
                limit_eh_s,
            )
        logger.info(f"Created marketplace order: {order_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to create order {order_id}: {e}")
        return False


async def get_order(pool: asyncpg.Pool, order_id: str) -> Optional[dict]:
    """
    Get a marketplace order by ID.

    Args:
        pool: asyncpg connection pool
        order_id: Order identifier

    Returns:
        Order dict or None if not found
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT order_id, host, port, username, password,
                       price_btc_eh_day, amount_btc, balance_btc, limit_eh_s,
                       created_at, updated_at, status,
                       fulfilled_at, paused_at, cancelled_at,
                       total_shares_credited, total_share_value_credited, last_share_credited_at
                FROM marketplace_orders WHERE order_id = $1
                """,
                order_id,
            )

            if row:
                return {
                    "order_id": row["order_id"],
                    "host": row["host"],
                    "port": row["port"],
                    "user": row["username"],
                    "password": row["password"],
                    "price_btc_eh_day": row["price_btc_eh_day"],
                    "amount_btc": row["amount_btc"],
                    "balance_btc": row["balance_btc"],
                    "limit_eh_s": row["limit_eh_s"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "status": row["status"],
                    "fulfilled_at": row["fulfilled_at"],
                    "paused_at": row["paused_at"],
                    "cancelled_at": row["cancelled_at"],
                    "total_shares_credited": row["total_shares_credited"],
                    "total_share_value_credited": row["total_share_value_credited"],
                    "last_share_credited_at": row["last_share_credited_at"],
                }
            return None
    except Exception as e:
        logger.error(f"Failed to get order {order_id}: {e}")
        return None


async def get_orders(pool: asyncpg.Pool, status: Optional[str] = None) -> list[dict]:
    """
    Get all marketplace orders, optionally filtered by status.

    Args:
        pool: asyncpg connection pool
        status: Optional filter by status ('active', 'paused', 'fulfilled', 'cancelled')

    Returns:
        List of order dicts
    """
    try:
        async with pool.acquire() as conn:
            base_query = """
                SELECT order_id, host, port, username, password,
                       price_btc_eh_day, amount_btc, balance_btc, limit_eh_s,
                       created_at, updated_at, status,
                       fulfilled_at, paused_at, cancelled_at,
                       total_shares_credited, total_share_value_credited, last_share_credited_at
                FROM marketplace_orders
            """
            if status:
                rows = await conn.fetch(
                    base_query + " WHERE status = $1 ORDER BY created_at DESC",
                    status,
                )
            else:
                rows = await conn.fetch(base_query + " ORDER BY created_at DESC")

            orders = []
            for row in rows:
                orders.append(
                    {
                        "order_id": row["order_id"],
                        "host": row["host"],
                        "port": row["port"],
                        "user": row["username"],
                        "password": row["password"],
                        "price_btc_eh_day": row["price_btc_eh_day"],
                        "amount_btc": row["amount_btc"],
                        "balance_btc": row["balance_btc"],
                        "limit_eh_s": row["limit_eh_s"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "status": row["status"],
                        "fulfilled_at": row["fulfilled_at"],
                        "paused_at": row["paused_at"],
                        "cancelled_at": row["cancelled_at"],
                        "total_shares_credited": row["total_shares_credited"],
                        "total_share_value_credited": row["total_share_value_credited"],
                        "last_share_credited_at": row["last_share_credited_at"],
                    }
                )
            return orders
    except Exception as e:
        logger.error(f"Failed to get orders: {e}")
        return []


async def update_order_status(pool: asyncpg.Pool, order_id: str, status: str) -> bool:
    """
    Update the status of a marketplace order.

    Args:
        pool: asyncpg connection pool
        order_id: Order identifier
        status: New status ('active', 'paused', 'fulfilled', 'cancelled')

    Returns:
        True if updated successfully, False otherwise
    """
    try:
        async with pool.acquire() as conn:
            if status == "cancelled":
                result = await conn.execute(
                    """
                    UPDATE marketplace_orders
                    SET status = $1, updated_at = NOW(), cancelled_at = NOW()
                    WHERE order_id = $2
                    """,
                    status,
                    order_id,
                )
            elif status == "fulfilled":
                result = await conn.execute(
                    """
                    UPDATE marketplace_orders
                    SET status = $1, updated_at = NOW(), fulfilled_at = NOW()
                    WHERE order_id = $2
                    """,
                    status,
                    order_id,
                )
            elif status == "paused":
                result = await conn.execute(
                    """
                    UPDATE marketplace_orders
                    SET status = $1, updated_at = NOW(), paused_at = NOW()
                    WHERE order_id = $2
                    """,
                    status,
                    order_id,
                )
            elif status == "active":
                # Resuming from paused - clear paused_at
                result = await conn.execute(
                    """
                    UPDATE marketplace_orders
                    SET status = $1, updated_at = NOW(), paused_at = NULL
                    WHERE order_id = $2
                    """,
                    status,
                    order_id,
                )
            else:
                result = await conn.execute(
                    """
                    UPDATE marketplace_orders
                    SET status = $1, updated_at = NOW()
                    WHERE order_id = $2
                    """,
                    status,
                    order_id,
                )
            updated = result != "UPDATE 0"
            if updated:
                logger.info(f"Updated order {order_id} status to {status}")
            return updated
    except Exception as e:
        logger.error(f"Failed to update order {order_id} status: {e}")
        return False


async def get_active_orders(pool: asyncpg.Pool) -> list[dict]:
    """
    Get all orders with 'active' status for balance processing.

    Args:
        pool: asyncpg connection pool

    Returns:
        List of active order dicts
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT order_id, host, port, username, password,
                       price_btc_eh_day, amount_btc, balance_btc, limit_eh_s,
                       created_at, updated_at, status,
                       fulfilled_at, paused_at, cancelled_at,
                       total_shares_credited, total_share_value_credited, last_share_credited_at
                FROM marketplace_orders
                WHERE status = 'active' AND balance_btc > 0
                ORDER BY created_at ASC
                """
            )

            orders = []
            for row in rows:
                orders.append(
                    {
                        "order_id": row["order_id"],
                        "host": row["host"],
                        "port": row["port"],
                        "user": row["username"],
                        "password": row["password"],
                        "price_btc_eh_day": row["price_btc_eh_day"],
                        "amount_btc": row["amount_btc"],
                        "balance_btc": row["balance_btc"],
                        "limit_eh_s": row["limit_eh_s"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "status": row["status"],
                        "fulfilled_at": row["fulfilled_at"],
                        "paused_at": row["paused_at"],
                        "cancelled_at": row["cancelled_at"],
                        "total_shares_credited": row["total_shares_credited"],
                        "total_share_value_credited": row["total_share_value_credited"],
                        "last_share_credited_at": row["last_share_credited_at"],
                    }
                )
            return orders
    except Exception as e:
        logger.error(f"Failed to get active orders: {e}")
        return []


async def create_order_with_assignments(
    pool: asyncpg.Pool,
    order_id: str,
    host: str,
    port: int,
    user: str,
    password: str,
    price_btc_eh_day: Decimal,
    amount_btc: Decimal,
    limit_eh_s: Optional[Decimal],
    worker_ips: list[str],
) -> dict:
    """
    Atomically create order and assign miners in a single transaction.

    Uses SELECT FOR UPDATE to prevent race conditions.

    Args:
        pool: asyncpg connection pool
        order_id: Unique order identifier
        host: Pool host address
        port: Pool port
        user: Pool username
        password: Pool password
        price_btc_eh_day: Price in BTC per Exahash per Day
        amount_btc: Total budget in BTC (initial balance)
        limit_eh_s: Optional hashrate speed cap in EH/s
        worker_ips: List of miner identifiers to assign

    Returns:
        {"assigned": [...], "skipped": [...]} on success

    Raises:
        ValueError: If order already exists
        ConflictError: If any miners are assigned to different orders
    """
    from src.api.services.market_assignment_queries import ConflictError

    worker_ips = list(dict.fromkeys(worker_ips))

    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchval(
                "SELECT 1 FROM marketplace_orders WHERE order_id = $1",
                order_id,
            )
            if existing:
                raise ValueError(f"Order {order_id} already exists")

            # Create order with share-based pricing
            await conn.execute(
                """
                INSERT INTO marketplace_orders (
                    order_id, host, port, username, password,
                    price_btc_eh_day, amount_btc, balance_btc, limit_eh_s, status
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $7, $8, 'active')
                """,
                order_id,
                host,
                port,
                user,
                password,
                price_btc_eh_day,
                amount_btc,
                limit_eh_s,
            )

            if not worker_ips:
                return {"assigned": [], "skipped": []}

            existing_assignments = await conn.fetch(
                """
                SELECT worker_ip, order_id
                FROM miner_assignments
                WHERE worker_ip = ANY($1) AND status = 'active'
                FOR UPDATE
                """,
                worker_ips,
            )

            conflicts = [
                {"worker_ip": row["worker_ip"], "current_order": row["order_id"]}
                for row in existing_assignments
            ]

            if conflicts:
                raise ConflictError(
                    "Some miners are already assigned to other orders",
                    conflicts=conflicts,
                )

            await conn.executemany(
                "INSERT INTO miner_assignments (worker_ip, order_id) VALUES ($1, $2)",
                [(w, order_id) for w in worker_ips],
            )

            logger.info(f"Created order {order_id} with {len(worker_ips)} miners")
            return {"assigned": worker_ips, "skipped": []}
