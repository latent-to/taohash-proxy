"""Allocator telemetry.

This module publishes allocator state and decision summaries to
Redis so we can observe behavior in real time.
"""

from __future__ import annotations

import json

from src.marketplace_allocator.types import TickDiagnostics
from src.utils.logger import get_logger
from src.utils.redis_client import RedisClient

logger = get_logger(__name__)


_KEY_LAST_TICK = "market:allocator:last_tick"


def _diag_to_dict(diag: TickDiagnostics) -> dict:
    return {
        "tick_ts": diag.tick_ts.isoformat(),
        "dt_seconds": diag.dt_seconds,
        "alpha": diag.alpha,
        "supply_eh_s": diag.supply_eh_s,
        "miner_count": diag.miner_count,
        "eligible_order_count": diag.eligible_order_count,
        "hard_drain_order_count": diag.hard_drain_order_count,
        "planned_switches": diag.planned_switches,
        "orders": [
            {
                "order_id": o.order_id,
                "eligible": o.eligible,
                "ineligible_reason": o.ineligible_reason,
                "price_btc_eh_day": str(o.price_btc_eh_day),
                "balance_btc": str(o.balance_btc),
                "limit_eh_s": str(o.limit_eh_s) if o.limit_eh_s is not None else None,
                "target_eh_s": o.target_eh_s,
                "current_eh_s": o.current_eh_s,
                "budget_eh_s": o.budget_eh_s,
                "hard_drain": o.hard_drain,
            }
            for o in diag.orders
        ],
        "events": [
            {
                "ts": e.ts.isoformat(),
                "level": e.level,
                "code": e.code,
                "message": e.message,
                "data": e.data,
            }
            for e in diag.events
        ],
    }


async def publish_last_tick(redis_client: RedisClient, diag: TickDiagnostics) -> None:
    """Publish the latest tick diagnostics to Redis."""

    redis = getattr(redis_client, "redis", None)
    if not redis:
        return
    try:
        payload = json.dumps(_diag_to_dict(diag))
        await redis.set(_KEY_LAST_TICK, payload, ex=600)
    except Exception as e:
        logger.error("Failed to publish allocator telemetry: %s", e)
