"""Configuration loading for the marketplace allocator."""

from __future__ import annotations

import dataclasses
from typing import Optional

from src.marketplace_allocator.types import AllocatorConfig
from src.utils.env import env_bool, env_float, env_int
from src.utils.logger import get_logger

logger = get_logger(__name__)


def load_config(poll_seconds_default: int) -> AllocatorConfig:
    """Load allocator configuration from environment variables.

    Called at the start of each loop iteration to pick up runtime changes.

    Args:
        poll_seconds_default: Default poll interval in seconds.

    Returns:
        Allocator configuration.
    """
    return AllocatorConfig(
        poll_seconds=env_int("ORDER_ALLOCATOR_POLL_SECONDS", poll_seconds_default),
        rebalance_rate=env_float("ORDER_ALLOCATOR_REBALANCE_RATE", 0.20),
        interval_seconds=env_int("ORDER_ALLOCATOR_INTERVAL_SECONDS", 300),
        overshoot_pct=env_float("ORDER_ALLOCATOR_OVERSHOOT_PCT", 0.15),
        miner_cooldown_seconds=env_int("ORDER_ALLOCATOR_MINER_COOLDOWN_SECONDS", 600),
        max_switches_per_tick=env_int("ORDER_ALLOCATOR_MAX_SWITCHES_PER_TICK", 50),
        enforce_hashprice_floor=env_bool(
            "ORDER_ALLOCATOR_ENFORCE_HASHPRICE_FLOOR", default=True
        ),
        rebalance_threshold_eh_s=env_float("ORDER_ALLOCATOR_DELTA_DEADBAND_EH_S", 0.0),
        enable_pool_health=env_bool("ORDER_ALLOCATOR_ENABLE_POOL_HEALTH", default=True),
        pool_health_warmup_seconds=env_int(
            "ORDER_ALLOCATOR_POOL_HEALTH_WARMUP_SECONDS", 120
        ),
        pool_health_min_samples=env_int("ORDER_ALLOCATOR_POOL_HEALTH_MIN_SAMPLES", 20),
        pool_health_no_notify_seconds=env_int(
            "ORDER_ALLOCATOR_POOL_HEALTH_NO_NOTIFY_SECONDS", 120
        ),
        pool_health_other_reject_threshold=env_float(
            "ORDER_ALLOCATOR_POOL_HEALTH_OTHER_REJECT_THRESHOLD", 0.20
        ),
        pool_health_reject_threshold=env_float(
            "ORDER_ALLOCATOR_POOL_HEALTH_REJECT_THRESHOLD", 0.50
        ),
        pool_health_ema_alpha=env_float("ORDER_ALLOCATOR_POOL_HEALTH_EMA_ALPHA", 0.30),
        pool_health_backoff_base_seconds=env_int(
            "ORDER_ALLOCATOR_POOL_HEALTH_BACKOFF_BASE_SECONDS", 120
        ),
        pool_health_backoff_max_seconds=env_int(
            "ORDER_ALLOCATOR_POOL_HEALTH_BACKOFF_MAX_SECONDS", 900
        ),
    )


def log_config_changes(
    old_config: Optional[AllocatorConfig], new_config: AllocatorConfig
) -> None:
    """Log any config fields that changed between ticks."""
    if old_config is None:
        return

    for field in dataclasses.fields(AllocatorConfig):
        old_value = getattr(old_config, field.name)
        new_value = getattr(new_config, field.name)
        if old_value != new_value:
            logger.warning(f"Config changed: {field.name} {old_value} -> {new_value}")
