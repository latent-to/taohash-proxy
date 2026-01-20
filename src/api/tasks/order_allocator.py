"""
TaoHash Marketplace Order Allocator Background Task.

This task runs in the API service and acts as the control plane* for
the TaoHash hashrate marketplace:

  * reads the current state (orders from Postgres, miners from Redis heartbeats)
  * runs the allocator rules and planner
  * applies the resulting plan by updating DB assignments and publishing
    switch commands to proxies via Redis pub/sub

The core logic lives in the `src.marketplace_allocator` package.
"""

from __future__ import annotations

import asyncio
import os
import zlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from src.api.services.market_order_queries import get_orders
from src.marketplace_allocator.executor import PlanExecutor
from src.marketplace_allocator.planner import plan_tick
from src.marketplace_allocator.rules import PlanningContext, default_rules
from src.marketplace_allocator.runtime_state import AllocatorRuntimeState
from src.marketplace_allocator.telemetry import publish_last_tick
from src.marketplace_allocator.pool_health import evaluate_pool_health
from src.marketplace_allocator.types import (
    AllocationSnapshot,
    AllocatorConfig,
    MinerSnapshot,
    OrderSnapshot,
    gh_to_eh,
)
from src.storage.marketplace_db import MarketplaceDB
from src.utils.env import env_bool
from src.utils.logger import get_logger
from src.utils.redis_client import RedisClient, RedisPublisher

logger = get_logger(__name__)


def _parse_decimal(value: object) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None


def _allocator_lock_key() -> int:
    """Return a stable 32-bit advisory lock key for Postgres."""

    return zlib.crc32(b"taohash:marketplace_allocator")


async def _run_once(
    *,
    marketplace_db: MarketplaceDB,
    redis_client: RedisClient,
    redis_publisher: RedisPublisher,
    config: AllocatorConfig,
    runtime_state: AllocatorRuntimeState,
    executor: PlanExecutor,
) -> None:
    now = datetime.now(timezone.utc)

    # 1) Snapshot orders
    order_rows = await get_orders(marketplace_db.pool, status="active")
    orders: list[OrderSnapshot] = []
    for row in order_rows:
        try:
            orders.append(
                OrderSnapshot(
                    order_id=row["order_id"],
                    host=row["host"],
                    port=int(row["port"]),
                    user=row["user"],
                    password=row["password"],
                    price_btc_eh_day=row["price_btc_eh_day"],
                    balance_btc=row["balance_btc"],
                    limit_eh_s=row.get("limit_eh_s"),
                    queue_position_time=row["queue_position_time"],
                    status=row["status"],
                )
            )
        except Exception:
            continue

    # 2) Snapshot miners (from Redis heartbeats)
    miners_stats = await redis_client.get_all_miners_with_stats()
    miners: list[MinerSnapshot] = []
    for worker_ip, stats in miners_stats.items():
        try:
            hashrate_gh = float(stats.get("hashrate_gh") or 0.0)
            rb = stats.get("rejected_breakdown") or {}
            miners.append(
                MinerSnapshot(
                    worker_ip=worker_ip,
                    hashrate_eh_s=gh_to_eh(hashrate_gh),
                    pool_label=str(stats.get("pool_label") or "unknown"),
                    accepted=int(stats.get("accepted") or 0),
                    rejected=int(stats.get("rejected") or 0),
                    rejected_stale=int(rb.get("stale") or 0),
                    rejected_duplicate=int(rb.get("duplicate") or 0),
                    rejected_low_diff=int(rb.get("low_diff") or 0),
                    rejected_other=int(rb.get("other") or 0),
                    last_notify_age_s=(
                        float(stats.get("last_notify_age_s"))
                        if stats.get("last_notify_age_s") is not None
                        else None
                    ),
                    worker_name=str(stats.get("worker_name") or ""),
                )
            )
        except Exception:
            continue

    # 3) Hashprice
    hashprice_btc_eh_day: Optional[Decimal] = None
    if config.enforce_hashprice_floor:
        hashprice_payload = await redis_client.get_hashprice_cache()
        if hashprice_payload:
            hashprice_btc_eh_day = _parse_decimal(hashprice_payload.get("btc_eh_day"))

    snapshot = AllocationSnapshot(
        now=now,
        orders=orders,
        miners=miners,
        hashprice_btc_eh_day=hashprice_btc_eh_day,
    )

    # 4) Load runtime state (cooldowns, last_tick, pool health state)
    worker_ips = [m.worker_ip for m in miners]
    order_ids: set[str] = {o.order_id for o in orders}
    # Include any currently mined non-default labels so backoff/warmup state is stable
    for m in miners:
        if m.pool_label not in config.default_labels:
            order_ids.add(str(m.pool_label))

    runtime_view = await runtime_state.load_view(
        worker_ips, order_ids=sorted(order_ids)
    )

    # 4b) Evaluate pool health and persist it (best effort).
    try:
        decision = evaluate_pool_health(
            now=now,
            miners=miners,
            prev_order_health=runtime_view.order_health,
            prev_miner_counters=runtime_view.miner_counters,
            config=config,
        )
        await runtime_state.persist_miner_counters(decision.miner_counters)
        await runtime_state.persist_order_health(decision.order_health)
    except Exception as e:
        logger.error("Pool health evaluation failed: %s", e)
        decision = None

    # 5) Apply rules
    ctx = PlanningContext(snapshot=snapshot, config=config, runtime=runtime_view)
    if decision is not None:
        ctx.order_health = decision.order_health
        # Attach health events for telemetry
        for ev in decision.events:
            try:
                data = dict(ev.get("data") or {})
                if ev.get("order_id"):
                    data.setdefault("order_id", ev.get("order_id"))
                ctx.add_event(
                    ev.get("level", "info"),
                    ev.get("code", "POOL_HEALTH"),
                    ev.get("message", "Pool health event"),
                    **data,
                )
            except Exception:
                continue
    for rule in default_rules():
        rule.apply(ctx)

    # 6) Plan
    plan, diag = plan_tick(ctx)

    # 7) Execute (DB + Redis) and persist runtime state
    await executor.apply(snapshot, plan)

    # 8) Telemetry
    await publish_last_tick(redis_client, diag)

    # Tick summary log
    if diag.planned_switches:
        logger.info(
            "Allocator tick: miners=%s supply_eh_s=%.6f eligible=%s hard_drain=%s moves=%s alpha=%.3f",
            diag.miner_count,
            diag.supply_eh_s,
            diag.eligible_order_count,
            diag.hard_drain_order_count,
            diag.planned_switches,
            diag.alpha,
        )

