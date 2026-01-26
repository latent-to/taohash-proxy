"""
Marketplace service.

Runs the allocator, share processing, and hashprice monitoring loops.
"""

import asyncio
import signal

from aiohttp import web

from src.shared.connections import (
    create_marketplace_db,
    create_redis_client,
    create_redis_publisher,
)
from src.api.tasks.order_share_processing import order_share_processing_loop
from src.api.tasks.order_allocator import order_allocator_loop
from src.api.tasks.hashprice_monitor import hashprice_monitor_loop
from src.utils.env import env_int
from src.utils.logger import get_logger

logger = get_logger(__name__)

_marketplace_db = None
_redis_client = None
_worker_tasks: list[asyncio.Task] = []


async def health_handler(request: web.Request) -> web.Response:
    """Health check endpoint for market service."""
    tasks_running = all(not t.done() for t in _worker_tasks) if _worker_tasks else False

    task_details = []
    for task in _worker_tasks:
        exc_str = None
        if task.done() and not task.cancelled():
            try:
                exc = task.exception()
                exc_str = str(exc) if exc else None
            except Exception:
                exc_str = "<unavailable>"

        task_details.append(
            {
                "name": getattr(task, "get_name", lambda: "<unnamed>")(),
                "done": task.done(),
                "cancelled": task.cancelled(),
                "exception": exc_str,
            }
        )

    db_ok = False
    if (
        _marketplace_db is not None
        and getattr(_marketplace_db, "pool", None) is not None
    ):
        try:
            async with _marketplace_db.pool.acquire() as conn:
                await conn.execute("SELECT 1")
            db_ok = True
        except Exception:
            db_ok = False

    redis_ok = False
    if _redis_client is not None and getattr(_redis_client, "redis", None) is not None:
        try:
            await _redis_client.redis.ping()
            redis_ok = True
        except Exception:
            redis_ok = False

    all_ok = tasks_running and db_ok and redis_ok
    status = "ok" if all_ok else "degraded"
    http_status = 200 if all_ok else 503

    return web.json_response(
        {
            "status": status,
            "all_ok": all_ok,
            "tasks_running": tasks_running,
            "tasks": task_details,
            "database": "connected" if db_ok else "disconnected",
            "redis": "connected" if redis_ok else "disconnected",
        },
        status=http_status,
    )


async def start_health_server() -> tuple[web.AppRunner, web.TCPSite]:
    """Start the health check HTTP server on port 8001."""
    health_app = web.Application()
    health_app.router.add_get("/health", health_handler)

    runner = web.AppRunner(health_app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", 8001)
    await site.start()

    logger.info("Health endpoint available at http://0.0.0.0:8001/health")
    return runner, site


async def main() -> None:
    """Main entry point for the market service."""
    global _marketplace_db, _redis_client, _worker_tasks

    poll_seconds = env_int("ORDER_BILLING_POLL_SECONDS", 30)

    logger.info("Initializing connections...")
    _marketplace_db = await create_marketplace_db()
    _redis_client = await create_redis_client()
    redis_publisher = await create_redis_publisher()

    logger.info("Starting health server...")
    health_runner, _ = await start_health_server()

    logger.info("Starting market service tasks...")

    logger.info("Creating worker tasks...")
    _worker_tasks = [
        asyncio.create_task(
            order_share_processing_loop(
                _marketplace_db, _redis_client, redis_publisher, poll_seconds
            ),
            name="share_processing",
        ),
        asyncio.create_task(
            hashprice_monitor_loop(_redis_client),
            name="hashprice_monitor",
        ),
        asyncio.create_task(
            order_allocator_loop(
                _marketplace_db, _redis_client, redis_publisher, poll_seconds
            ),
            name="order_allocator",
        ),
    ]

    logger.info(
        f"Market service started with {len(_worker_tasks)} tasks "
        f"(poll_seconds={poll_seconds})"
    )

    shutdown_event = asyncio.Event()

    def handle_signal() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    try:
        await shutdown_event.wait()
    finally:
        logger.info("Cancelling market tasks...")
        for task in _worker_tasks:
            task.cancel()
        await asyncio.gather(*_worker_tasks, return_exceptions=True)

        await health_runner.cleanup()

        logger.info("Closing connections...")
        await redis_publisher.close()
        await _redis_client.close()
        await _marketplace_db.close()

        logger.info("Market service stopped")


if __name__ == "__main__":
    asyncio.run(main())
