from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, List

from src.difficulty_monitoring.monitor import (
    difficulty_monitor_task,
    difficulty_monitor_task_bch,
)
from src.tides_monitoring.monitor import tides_monitor_task, tides_monitor_task_bch
from src.tides_monitoring.btc.ocean_rewards_monitor import (
    tides_rewards_ocean_monitor_task,
)
from src.tides_monitoring.bch.rewards_monitor import tides_rewards_monitor_task_bch
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class StartupFlags:
    rewards: bool
    difficulty: bool
    tides: bool
    tides_rewards: bool


def build_background_coroutines(coin: str, db, flags: StartupFlags) -> List[Awaitable]:
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

    filtered: List[Awaitable] = []

    for flag_name, label, func in task_map[coin]:
        enabled = getattr(flags, flag_name)
        if enabled:
            logger.info("%s is enabled", label)
            filtered.append(func(db))
        else:
            logger.info("%s is disabled", label)

    return filtered


def schedule_background_tasks(coros: List[Awaitable]) -> List[asyncio.Task]:
    """Create asyncio tasks from coroutine objects."""

    return [asyncio.create_task(coro) for coro in coros]
