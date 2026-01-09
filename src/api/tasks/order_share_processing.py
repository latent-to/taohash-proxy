"""
Background worker to process order share events from Redis Streams.

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
        worker_ip = assignment["worker_ip"]
        await redis_publisher.publish_switch_default(worker_ip)
        await delete_assignment(marketplace_db.pool, worker_ip)

    await update_order_status(marketplace_db.pool, order_id, "fulfilled")
    logger.info(
        "Order %s fulfilled; switched %s miners back", order_id, len(assignments)
    )


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

        async def process_messages(messages: list[tuple[str, dict]]) -> None:
            """Validate, aggregate, debit, and ack a batch of stream messages."""
            if not messages:
                return

            totals: dict[str, Decimal] = {}
            counts: dict[str, int] = {}
            order_message_ids: dict[str, list[str]] = {}
            ack_ids: list[str] = []

            for message_id, data in messages:
                # Filter out invalid or reserved order labels and bad difficulty.
                order_id = data.get("order_id")
                if not order_id or order_id in RESERVED_LABELS:
                    ack_ids.append(message_id)
                    continue

                difficulty_raw = data.get("difficulty")
                try:
                    difficulty = Decimal(str(difficulty_raw))
                except (InvalidOperation, TypeError):
                    ack_ids.append(message_id)
                    continue

                if difficulty <= 0:
                    ack_ids.append(message_id)
                    continue

                totals[order_id] = totals.get(order_id, Decimal("0")) + difficulty
                counts[order_id] = counts.get(order_id, 0) + 1
                order_message_ids.setdefault(order_id, []).append(message_id)

            for order_id, diff_sum in totals.items():
                # Fetch order, apply debit, and fulfill if balance is depleted.
                message_ids = order_message_ids.get(order_id, [])
                try:
                    order = await get_order(marketplace_db.pool, order_id)
                    if not order:
                        logger.warning(f"Order {order_id} not found")
                        ack_ids.extend(message_ids)
                        continue

                    if order["status"] == "cancelled":
                        ack_ids.extend(message_ids)
                        continue

                    price = order["price_btc_eh_day"]
                    if not price:
                        ack_ids.extend(message_ids)
                        continue

                    cost_btc = price * diff_sum * DIFF_TO_BTC_FACTOR
                    updated = await apply_share_credit(
                        marketplace_db.pool,
                        order_id,
                        counts.get(order_id, 0),
                        cost_btc,
                    )
                    if not updated:
                        # If the order was cancelled mid-flight, ack and drop.
                        latest = await get_order(marketplace_db.pool, order_id)
                        if not latest or latest["status"] == "cancelled":
                            ack_ids.extend(message_ids)
                        continue

                    if updated["balance_btc"] <= 0 and updated["status"] != "fulfilled":
                        await _fulfill_order(marketplace_db, redis_publisher, order_id)

                    ack_ids.extend(message_ids)
                except Exception as e:
                    logger.error(f"Order share processing failed for {order_id}: {e}")

            if ack_ids:
                await redis_client.ack_order_shares(ack_ids)

        while True:
            # Drain pending entries first; if none, block for new ones.
            pending = await redis_client.read_order_share_batch(
                ORDER_BILLING_BATCH_SIZE,
                1,
                read_id="0",
            )
            if pending:
                await process_messages(pending)
                continue

            messages = await redis_client.read_order_share_batch(
                ORDER_BILLING_BATCH_SIZE,
                block_ms,
            )
            await process_messages(messages)

    except asyncio.CancelledError:
        logger.info("Order share worker stopped")
        raise
