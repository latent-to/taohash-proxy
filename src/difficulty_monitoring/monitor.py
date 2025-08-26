"""Difficulty monitoring task for automatic network difficulty updates."""

import asyncio
import os

from src.api.services.config_queries import get_config, update_config
from src.difficulty_monitoring.braiins_provider import BraiinsDifficultyProvider
from src.storage.db import StatsDB
from src.utils.logger import get_logger

logger = get_logger(__name__)

DIFFICULTY_CHECK_INTERVAL_MINUTES = int(
    os.environ.get("DIFFICULTY_CHECK_INTERVAL", "30")
)


async def difficulty_monitor_task(db: StatsDB) -> None:
    """
    Periodic task to fetch network difficulty and update config.

    Args:
        db: Database connection
    """
    provider = BraiinsDifficultyProvider()
    interval_seconds = DIFFICULTY_CHECK_INTERVAL_MINUTES * 60

    logger.info(
        f"Starting difficulty monitoring (every {DIFFICULTY_CHECK_INTERVAL_MINUTES} minutes)"
    )

    while True:
        try:
            await _check_and_update_difficulty(db, provider)
        except Exception as e:
            logger.error(f"Error in difficulty monitoring: {e}")

        await asyncio.sleep(interval_seconds)


async def _check_and_update_difficulty(
    db: StatsDB, provider: BraiinsDifficultyProvider
) -> None:
    """Check difficulty and update config if needed."""

    latest_difficulty = await provider.get_network_difficulty()
    if latest_difficulty is None:
        logger.warning("Failed to fetch network difficulty, skipping update")
        return

    current_config = await get_config(db)
    if not current_config:
        logger.warning("No current config found, skipping difficulty update")
        return

    current_difficulty = current_config.get("network_difficulty")

    if current_difficulty == latest_difficulty:
        logger.debug(f"Network difficulty unchanged: {latest_difficulty:,.0f}")
        return

    try:
        await update_config(db, network_difficulty=latest_difficulty)
        logger.info(
            f"Updated network difficulty: {current_difficulty:,.0f} → {latest_difficulty:,.0f}"
        )
    except Exception as e:
        logger.error(f"Failed to update network difficulty: {e}")
