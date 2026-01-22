"""Marketplace order queries and atomic operations."""

from decimal import Decimal
from typing import Optional

import asyncpg

from src.marketplace_allocator.types import MarketplaceOrder
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


async def get_order(pool: asyncpg.Pool, order_id: str) -> Optional[MarketplaceOrder]:
    """
    Get a marketplace order by ID.

    Args:
        pool: asyncpg connection pool
        order_id: Order identifier

    Returns:
        MarketplaceOrder or None if not found
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT order_id, host, port, username, password,
                       price_btc_eh_day, amount_btc, balance_btc, limit_eh_s,
                       created_at, updated_at, queue_position_time, status,
                       fulfilled_at, paused_at, cancelled_at, last_rebalance_at,
                       total_shares_credited, total_share_value_credited, last_share_credited_at
                FROM marketplace_orders WHERE order_id = $1
                """,
                order_id,
            )

            if row:
                return MarketplaceOrder.from_db_row(row)
            return None
    except Exception as e:
        logger.error(f"Failed to get order {order_id}: {e}")
        return None


async def get_orders(
    pool: asyncpg.Pool, status: Optional[str] = None
) -> list[MarketplaceOrder]:
    """
    Get all marketplace orders, optionally filtered by status.

    Args:
        pool: asyncpg connection pool
        status: Optional filter by status ('active', 'paused', 'fulfilled', 'cancelled')

    Returns:
        List of MarketplaceOrder objects
    """
    try:
        async with pool.acquire() as conn:
            base_query = """
                SELECT order_id, host, port, username, password,
                       price_btc_eh_day, amount_btc, balance_btc, limit_eh_s,
                       created_at, updated_at, queue_position_time, status,
                       fulfilled_at, paused_at, cancelled_at, last_rebalance_at,
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

            return [MarketplaceOrder.from_db_row(row) for row in rows]
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
                    SET status = $1,
                        updated_at = NOW(),
                        paused_at = NULL,
                        queue_position_time = CASE
                            WHEN status = 'paused' THEN NOW()
                            ELSE queue_position_time
                        END
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


async def update_order_fields(
    pool: asyncpg.Pool,
    order_id: str,
    price_btc_eh_day: Optional[Decimal] = None,
    limit_eh_s: Optional[Decimal] = None,
    limit_provided: bool = False,
    topup_btc: Optional[Decimal] = None,
) -> bool:
    """
    Update mutable order fields like price, limit, and balance top-ups.

    Resets queue_position_time when price or limit is lowered, or when a
    fulfilled order is topped up and reactivated.
    """
    if (
        price_btc_eh_day is None
        and not limit_provided
        and (topup_btc is None or topup_btc <= 0)
    ):
        return False

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT price_btc_eh_day, limit_eh_s, amount_btc, balance_btc, status
                    FROM marketplace_orders
                    WHERE order_id = $1
                    FOR UPDATE
                    """,
                    order_id,
                )
                if not row:
                    return False
                if row["status"] == "cancelled":
                    raise ValueError(f"Order {order_id} is cancelled")

                update_fields: list[str] = []
                values: list[object] = []

                if price_btc_eh_day is not None:
                    update_fields.append(f"price_btc_eh_day = ${len(values) + 1}")
                    values.append(price_btc_eh_day)

                if limit_provided:
                    update_fields.append(f"limit_eh_s = ${len(values) + 1}")
                    values.append(limit_eh_s)

                if topup_btc is not None and topup_btc > 0:
                    update_fields.append(f"amount_btc = ${len(values) + 1}")
                    values.append(row["amount_btc"] + topup_btc)
                    update_fields.append(f"balance_btc = ${len(values) + 1}")
                    values.append(row["balance_btc"] + topup_btc)

                reset_queue = False
                if (
                    price_btc_eh_day is not None
                    and price_btc_eh_day < row["price_btc_eh_day"]
                ):
                    reset_queue = True

                if limit_provided:
                    current_limit = row["limit_eh_s"]
                    new_limit = limit_eh_s
                    if new_limit is not None:
                        if current_limit is None or new_limit < current_limit:
                            reset_queue = True

                if (
                    topup_btc is not None
                    and topup_btc > 0
                    and row["status"] == "fulfilled"
                ):
                    update_fields.append("status = 'active'")
                    update_fields.append("fulfilled_at = NULL")
                    reset_queue = True

                if reset_queue:
                    update_fields.append("queue_position_time = NOW()")

                update_fields.append("updated_at = NOW()")

                if not update_fields:
                    return False

                query = (
                    "UPDATE marketplace_orders SET "
                    + ", ".join(update_fields)
                    + f" WHERE order_id = ${len(values) + 1}"
                )
                values.append(order_id)
                result = await conn.execute(query, *values)

        return result != "UPDATE 0"
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Failed to update order {order_id}: {e}")
        return False


async def get_active_orders(pool: asyncpg.Pool) -> list[MarketplaceOrder]:
    """
    Get all orders with 'active' status for balance processing.

    Args:
        pool: asyncpg connection pool

    Returns:
        List of active MarketplaceOrder objects
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT order_id, host, port, username, password,
                       price_btc_eh_day, amount_btc, balance_btc, limit_eh_s,
                       created_at, updated_at, queue_position_time, status,
                       fulfilled_at, paused_at, cancelled_at, last_rebalance_at,
                       total_shares_credited, total_share_value_credited, last_share_credited_at
                FROM marketplace_orders
                WHERE status = 'active' AND balance_btc > 0
                ORDER BY created_at ASC
                """
            )

            return [MarketplaceOrder.from_db_row(row) for row in rows]
    except Exception as e:
        logger.error(f"Failed to get active orders: {e}")
        return []


async def apply_share_credit(
    pool: asyncpg.Pool,
    order_id: str,
    share_count: int,
    cost_btc: Decimal,
) -> Optional[dict]:
    """
    Apply share credit to an order by decrementing balance and updating totals.

    Returns:
        Dict with updated balance and status, or None if not updated
    """
    if share_count <= 0 or cost_btc <= 0:
        return None

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                 UPDATE marketplace_orders
                SET balance_btc = GREATEST(balance_btc - $1, 0),
                    total_shares_credited = total_shares_credited + $2,
                    total_share_value_credited = total_share_value_credited + $1,
                    last_share_credited_at = NOW(),
                    updated_at = NOW()
                WHERE order_id = $3 AND status != 'cancelled'
                RETURNING balance_btc, status
                """,
                cost_btc,
                share_count,
                order_id,
            )
        if row:
            logger.info(
                f"Applied share credit to {order_id}: {cost_btc} BTC for {share_count} shares. "
                f"Balance: {row['balance_btc']} BTC"
            )
            return {"balance_btc": row["balance_btc"], "status": row["status"]}
        return None
    except Exception as e:
        logger.error(f"Failed to apply share credit to {order_id}: {e}")
        return None


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
    miner_ids: list[str],
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
        miner_ids: List of miner identifiers to assign

    Returns:
        {"assigned": [...], "skipped": [...]} on success

    Raises:
        ValueError: If order already exists
        ConflictError: If any miners are assigned to different orders
    """
    from src.api.services.market_assignment_queries import ConflictError

    miner_ids = list(dict.fromkeys(miner_ids))

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

            if not miner_ids:
                return {"assigned": [], "skipped": []}

            existing_assignments = await conn.fetch(
                """
                SELECT miner_id, order_id
                FROM miner_assignments
                WHERE miner_id = ANY($1) AND status = 'active'
                FOR UPDATE
                """,
                miner_ids,
            )

            conflicts = [
                {"miner_id": row["miner_id"], "current_order": row["order_id"]}
                for row in existing_assignments
            ]

            if conflicts:
                raise ConflictError(
                    "Some miners are already assigned to other orders",
                    conflicts=conflicts,
                )

            await conn.executemany(
                "INSERT INTO miner_assignments (miner_id, order_id) VALUES ($1, $2)",
                [(w, order_id) for w in miner_ids],
            )

            logger.info(f"Created order {order_id} with {len(miner_ids)} miners")
            return {"assigned": miner_ids, "skipped": []}
