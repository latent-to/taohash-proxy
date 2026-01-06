"""Background task to monitor and expire marketplace orders."""

import asyncio

import asyncpg

from src.api.services.market_order_queries import (
    get_expired_orders,
    update_order_status,
)
from src.api.services.market_assignment_queries import (
    get_order_assignments,
    delete_assignment,
)
from src.utils.logger import get_logger
from src.utils.redis_client import RedisPublisher

logger = get_logger(__name__)


async def order_expiry_monitor_task(
    pool: asyncpg.Pool,
    redis_publisher: RedisPublisher,
) -> None:
    """
    Background task to check for expired orders and switch miners back.

    Runs every 10 seconds, checks all active orders for expiry.
    """
    logger.info("Order expiry monitor started")

    while True:
        await asyncio.sleep(10)

        try:
            expired_orders = await get_expired_orders(pool)

            for order in expired_orders:
                order_id = order["order_id"]
                logger.info(f"Order {order_id} expired")

                assignments = await get_order_assignments(pool, order_id)

                for assignment in assignments:
                    worker_ip = assignment["worker_ip"]
                    await redis_publisher.publish_switch_default(worker_ip)
                    await delete_assignment(pool, worker_ip)

                await update_order_status(pool, order_id, "expired")
                logger.info(
                    f"Expired order {order_id}, switched {len(assignments)} miners back"
                )

        except Exception as e:
            logger.error(f"Error in order expiry monitor: {e}")
