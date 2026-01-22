"""
Module to process order share events from Redis Streams.

Consumes accepted share events, aggregates difficulty per order, converts
to BTC cost, applies debits in Postgres, and fulfills orders when balance
reaches zero. Also drains pending stream entries on startup to avoid
stuck messages after crashes.
"""

import asyncio
from decimal import Decimal, InvalidOperation

from src.api.services.market_assignment_queries import (
    delete_assignment,
    get_order_assignments,
)
from src.api.services.market_order_queries import (
    apply_share_credit,
    get_order,
    update_order_status,
)
from src.storage.marketplace_db import MarketplaceDB
from src.utils.logger import get_logger
from src.utils.redis_client import RedisClient, RedisPublisher

logger = get_logger(__name__)

ORDER_BILLING_BATCH_SIZE = 200
DIFF_TO_BTC_FACTOR = Decimal("4294967296") / Decimal("1e18") / Decimal("86400")
RESERVED_LABELS = {"normal", "high_diff", "unknown"}


async def _fulfill_order(
    marketplace_db: MarketplaceDB,
    redis_publisher: RedisPublisher,
    order_id: str,
) -> None:
    """Switch all assigned miners back to default pool and mark order fulfilled."""
    assignments = await get_order_assignments(marketplace_db.pool, order_id)

    for assignment in assignments:
        miner_id = assignment["miner_id"]
        await redis_publisher.publish_switch_default(miner_id)
        await delete_assignment(marketplace_db.pool, miner_id)

    await update_order_status(marketplace_db.pool, order_id, "fulfilled")
    logger.info(
        "Order %s fulfilled; switched %s miners back", order_id, len(assignments)
    )


def _validate_and_aggregate_messages(
    messages: list[tuple[str, dict]],
) -> tuple[dict[str, Decimal], dict[str, int], dict[str, list[str]], list[str]]:
    """
    Validate and aggregate share difficulty by order.

    Returns:
        difficulty_by_order: Sum of share difficulty per order_id
        share_count_by_order: Number of valid shares per order_id
        message_ids_by_order: Stream message IDs grouped by order_id
        invalid_ack_ids: Message IDs to ack without processing (invalid/reserved)
    """
    difficulty_by_order: dict[str, Decimal] = {}
    share_count_by_order: dict[str, int] = {}
    message_ids_by_order: dict[str, list[str]] = {}
    invalid_ack_ids: list[str] = []

    for message_id, data in messages:
        order_id = data.get("order_id")
        if not order_id or order_id in RESERVED_LABELS:
            invalid_ack_ids.append(message_id)
            continue

        difficulty_raw = data.get("difficulty")
        try:
            difficulty = Decimal(str(difficulty_raw))
        except (InvalidOperation, TypeError):
            invalid_ack_ids.append(message_id)
            continue

        if difficulty <= 0:
            invalid_ack_ids.append(message_id)
            continue

        difficulty_by_order[order_id] = (
            difficulty_by_order.get(order_id, Decimal("0")) + difficulty
        )
        share_count_by_order[order_id] = share_count_by_order.get(order_id, 0) + 1
        message_ids_by_order.setdefault(order_id, []).append(message_id)

    return (
        difficulty_by_order,
        share_count_by_order,
        message_ids_by_order,
        invalid_ack_ids,
    )


async def _process_order_debits(
    difficulty_by_order: dict[str, Decimal],
    share_count_by_order: dict[str, int],
    message_ids_by_order: dict[str, list[str]],
    marketplace_db: MarketplaceDB,
    redis_publisher: RedisPublisher,
) -> list[str]:
    """
    Apply share credits to orders and fulfill if balance depleted.

    Returns: message IDs to acknowledge
    """
    ack_ids: list[str] = []

    for order_id, total_difficulty in difficulty_by_order.items():
        message_ids = message_ids_by_order.get(order_id, [])
        try:
            order = await get_order(marketplace_db.pool, order_id)
            if not order:
                logger.warning(f"Order {order_id} not found")
                ack_ids.extend(message_ids)
                continue

            if order.status == "cancelled":
                ack_ids.extend(message_ids)
                continue

            price = order.price_btc_eh_day
            if not price:
                ack_ids.extend(message_ids)
                continue

            cost_btc = price * total_difficulty * DIFF_TO_BTC_FACTOR
            updated = await apply_share_credit(
                marketplace_db.pool,
                order_id,
                share_count_by_order.get(order_id, 0),
                cost_btc,
            )
            if not updated:
                latest = await get_order(marketplace_db.pool, order_id)
                if not latest or latest.status == "cancelled":
                    ack_ids.extend(message_ids)
                continue

            if updated["balance_btc"] <= 0 and updated["status"] != "fulfilled":
                await _fulfill_order(marketplace_db, redis_publisher, order_id)

            ack_ids.extend(message_ids)
        except Exception as e:
            logger.error(f"Order share processing failed for {order_id}: {e}")

    return ack_ids


async def order_share_processing_loop(
    marketplace_db: MarketplaceDB,
    redis_client: RedisClient,
    redis_publisher: RedisPublisher,
    poll_seconds: int,
) -> None:
    """
    Consume order share events from Redis Streams and debit balances.

    Each loop prioritizes pending entries (read_id="0") to reclaim unacked
    messages, then blocks on new entries (read_id=">") for normal processing.
    """
    if not marketplace_db or not marketplace_db.pool:
        logger.warning("Order share worker disabled: marketplace DB unavailable")
        return

    if not redis_client:
        logger.warning("Order share worker disabled: Redis client unavailable")
        return

    if not await redis_client.ensure_connected():
        logger.warning("Order share worker disabled: Redis connection failed")
        return

    if not await redis_client.ensure_order_share_group():
        logger.warning("Order share worker disabled: failed to create stream group")
        return

    block_ms = max(1000, int(poll_seconds * 1000))
    logger.info(
        f"Order share worker started (poll={poll_seconds}s, batch={ORDER_BILLING_BATCH_SIZE})"
    )

    try:
        while True:
            pending = await redis_client.read_order_share_batch(
                ORDER_BILLING_BATCH_SIZE,
                1,
                read_id="0",
            )
            if pending:
                (
                    difficulty_by_order,
                    share_count_by_order,
                    message_ids_by_order,
                    ack_ids,
                ) = _validate_and_aggregate_messages(pending)
                order_ack_ids = await _process_order_debits(
                    difficulty_by_order,
                    share_count_by_order,
                    message_ids_by_order,
                    marketplace_db,
                    redis_publisher,
                )
                ack_ids.extend(order_ack_ids)
                if ack_ids:
                    await redis_client.ack_order_shares(ack_ids)
                continue

            messages = await redis_client.read_order_share_batch(
                ORDER_BILLING_BATCH_SIZE,
                block_ms,
            )
            if messages:
                (
                    difficulty_by_order,
                    share_count_by_order,
                    message_ids_by_order,
                    ack_ids,
                ) = _validate_and_aggregate_messages(messages)
                order_ack_ids = await _process_order_debits(
                    difficulty_by_order,
                    share_count_by_order,
                    message_ids_by_order,
                    marketplace_db,
                    redis_publisher,
                )
                ack_ids.extend(order_ack_ids)
                if ack_ids:
                    await redis_client.ack_order_shares(ack_ids)

    except asyncio.CancelledError:
        logger.info("Order share worker stopped")
        raise
