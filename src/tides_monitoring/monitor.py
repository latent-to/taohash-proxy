"""TIDES window monitoring task for automatic window updates."""

import asyncio
import os

from src.api.services.tides_queries import calculate_and_store_tides_window
from src.storage.db import StatsDB
from src.utils.logger import get_logger

logger = get_logger(__name__)

TIDES_CHECK_INTERVAL_MINUTES = int(
    os.environ.get("TIDES_CHECK_INTERVAL", "30")
)


async def tides_monitor_task(db: StatsDB) -> None:
    """
    Periodic task to calculate and cache TIDES window.

    Args:
        db: Database connection
    """
    interval_seconds = TIDES_CHECK_INTERVAL_MINUTES * 60

    logger.info(
        f"Starting TIDES monitoring (every {TIDES_CHECK_INTERVAL_MINUTES} minutes)"
    )

    while True:
        try:
            await calculate_and_store_tides_window(db)
            logger.info("TIDES window updated successfully")
        except Exception as e:
            logger.error(f"Error in TIDES monitoring: {e}")

        await asyncio.sleep(interval_seconds)