from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable

from src.difficulty_monitoring.monitor import (
    difficulty_monitor_task,
    difficulty_monitor_task_bch,
)
from src.tides_monitoring.monitor import tides_monitor_task, tides_monitor_task_bch
from src.tides_monitoring.btc.ocean_rewards_monitor import (
    tides_rewards_ocean_monitor_task,
)
from src.api.tasks.order_share_processing import order_share_processing_loop
from src.api.tasks.hashprice_monitor import hashprice_monitor_loop
from src.api.tasks.order_allocator import order_allocator_loop
from src.tides_monitoring.bch.rewards_monitor import tides_rewards_monitor_task_bch
from src.utils.logger import get_logger
from src.utils.env import env_bool

logger = get_logger(__name__)


@dataclass
class StartupFlags:
    rewards: bool
    difficulty: bool
    tides: bool
    tides_rewards: bool


def build_background_coroutines(
    coin: str,
    db,
    flags: StartupFlags,
) -> list[Awaitable]:
    """Return coroutine objects for background tasks."""

    coin = coin.lower()

    task_map = {
        "btc": [
            ("difficulty", "Difficulty monitoring BTC", difficulty_monitor_task),
            ("tides", "TIDES window monitoring BTC", tides_monitor_task),
            (
                "tides_rewards",
                "TIDES rewards monitoring (Ocean BTC)",
                tides_rewards_ocean_monitor_task,
            ),
        ],
        "bch": [
            (
                "difficulty",
                "Difficulty monitoring (BCH)",
                difficulty_monitor_task_bch,
            ),
            ("tides", "TIDES window monitoring (BCH)", tides_monitor_task_bch),
            (
                "tides_rewards",
                "TIDES rewards monitoring (BCH - CryptoAPIs)",
                tides_rewards_monitor_task_bch,
            ),
        ],
    }

    if coin not in task_map:
        logger.warning("Unsupported coin '%s'. No background tasks scheduled.", coin)
        return []

    filtered: list[Awaitable] = []

    for flag_name, label, func in task_map[coin]:
        enabled = getattr(flags, flag_name)
        if enabled:
            logger.info("%s is enabled", label)
            filtered.append(func(db))
        else:
            logger.info("%s is disabled", label)

    return filtered


def build_marketplace_coroutines(
    marketplace_db,
    redis_client,
    redis_publisher,
    poll_seconds: int,
) -> list[Awaitable]:
    """Return coroutine objects for marketplace background tasks."""

    if not marketplace_db or not redis_client or not redis_publisher:
        logger.info("Marketplace background tasks disabled: missing dependencies")
        return []

    coros: list[Awaitable] = [
        order_share_processing_loop(
            marketplace_db,
            redis_client,
            redis_publisher,
            poll_seconds,
        ),
        hashprice_monitor_loop(redis_client),
    ]

    if env_bool("ENABLE_MARKETPLACE_ALLOCATOR", default=False):
        coros.append(
            order_allocator_loop(
                marketplace_db,
                redis_client,
                redis_publisher,
                poll_seconds,
            )
        )
    else:
        logger.info(
            "Marketplace allocator is disabled (set ENABLE_MARKETPLACE_ALLOCATOR=true to enable)"
        )

    return coros


def schedule_background_tasks(coros: list[Awaitable]) -> list[asyncio.Task]:
    """Create asyncio tasks from coroutine objects."""

    return [asyncio.create_task(coro) for coro in coros]
