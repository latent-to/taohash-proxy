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


async def order_allocator_loop(
    marketplace_db: MarketplaceDB,
    redis_client: RedisClient,
    redis_publisher: RedisPublisher,
    poll_seconds: int,
) -> None:
    """Main allocator loop.

    Enabled by env var:
      ENABLE_MARKETPLACE_ALLOCATOR=true

    Optional tuning env vars:
      ORDER_ALLOCATOR_POLL_SECONDS
      ORDER_ALLOCATOR_P
      ORDER_ALLOCATOR_INTERVAL_SECONDS
      ORDER_ALLOCATOR_OVERSHOOT_PCT
      ORDER_ALLOCATOR_MINER_COOLDOWN_SECONDS
      ORDER_ALLOCATOR_MAX_SWITCHES_PER_TICK
      ORDER_ALLOCATOR_ENFORCE_HASHPRICE_FLOOR
      ORDER_ALLOCATOR_DELTA_DEADBAND_EH_S
    """

    if not env_bool("ENABLE_MARKETPLACE_ALLOCATOR", default=False):
        logger.info(
            "Order allocator disabled (set ENABLE_MARKETPLACE_ALLOCATOR=true to enable)"
        )
        return

    if not marketplace_db or not marketplace_db.pool:
        logger.warning("Order allocator disabled: marketplace DB unavailable")
        return

    if not redis_client or not redis_publisher:
        logger.warning("Order allocator disabled: Redis unavailable")
        return

    # Config
    poll = int(os.getenv("ORDER_ALLOCATOR_POLL_SECONDS", str(poll_seconds)))
    p = float(os.getenv("ORDER_ALLOCATOR_P", "0.20"))
    interval = int(os.getenv("ORDER_ALLOCATOR_INTERVAL_SECONDS", "300"))
    overshoot = float(os.getenv("ORDER_ALLOCATOR_OVERSHOOT_PCT", "0.15"))
    cooldown = int(os.getenv("ORDER_ALLOCATOR_MINER_COOLDOWN_SECONDS", "600"))
    max_switches = int(os.getenv("ORDER_ALLOCATOR_MAX_SWITCHES_PER_TICK", "50"))
    enforce_floor = env_bool("ORDER_ALLOCATOR_ENFORCE_HASHPRICE_FLOOR", default=False)
    deadband = float(os.getenv("ORDER_ALLOCATOR_DELTA_DEADBAND_EH_S", "0"))

    # Pool health tuning
    enable_pool_health = env_bool("ORDER_ALLOCATOR_ENABLE_POOL_HEALTH", default=True)
    health_warmup = int(os.getenv("ORDER_ALLOCATOR_POOL_HEALTH_WARMUP_SECONDS", "120"))
    health_min_samples = int(os.getenv("ORDER_ALLOCATOR_POOL_HEALTH_MIN_SAMPLES", "20"))
    health_no_notify = int(
        os.getenv("ORDER_ALLOCATOR_POOL_HEALTH_NO_NOTIFY_SECONDS", "120")
    )
    health_other_reject = float(
        os.getenv("ORDER_ALLOCATOR_POOL_HEALTH_OTHER_REJECT_THRESHOLD", "0.20")
    )
    health_reject = float(
        os.getenv("ORDER_ALLOCATOR_POOL_HEALTH_REJECT_THRESHOLD", "0.50")
    )
    health_ema = float(os.getenv("ORDER_ALLOCATOR_POOL_HEALTH_EMA_ALPHA", "0.30"))
    health_backoff_base = int(
        os.getenv("ORDER_ALLOCATOR_POOL_HEALTH_BACKOFF_BASE_SECONDS", "120")
    )
    health_backoff_max = int(
        os.getenv("ORDER_ALLOCATOR_POOL_HEALTH_BACKOFF_MAX_SECONDS", "900")
    )

    config = AllocatorConfig(
        poll_seconds=poll,
        rebalance_rate=p,
        interval_seconds=interval,
        overshoot_pct=overshoot,
        miner_cooldown_seconds=cooldown,
        max_switches_per_tick=max_switches,
        enforce_hashprice_floor=enforce_floor,
        rebalance_threshold_eh_s=deadband,
        enable_pool_health=enable_pool_health,
        pool_health_warmup_seconds=health_warmup,
        pool_health_min_samples=health_min_samples,
        pool_health_no_notify_seconds=health_no_notify,
        pool_health_other_reject_threshold=health_other_reject,
        pool_health_reject_threshold=health_reject,
        pool_health_ema_alpha=health_ema,
        pool_health_backoff_base_seconds=health_backoff_base,
        pool_health_backoff_max_seconds=health_backoff_max,
    )

    # Runtime state + executor
    runtime_state = AllocatorRuntimeState(redis_client)
    executor = PlanExecutor(marketplace_db, redis_publisher, runtime_state)

    lock_key = _allocator_lock_key()
    logger.info(
        "Order allocator starting (poll=%ss, p=%.2f, interval=%ss, max_switches=%s)",
        config.poll_seconds,
        config.rebalance_rate,
        config.interval_seconds,
        config.max_switches_per_tick,
    )

    # Leader lock: only one allocator should be active
    while True:
        try:
            async with marketplace_db.pool.acquire() as conn:
                got_lock = await conn.fetchval(
                    "SELECT pg_try_advisory_lock($1)",
                    lock_key,
                )

                if not got_lock:
                    logger.debug("Allocator follower: lock held by another instance")
                    await asyncio.sleep(config.poll_seconds)
                    continue

                logger.info("Allocator leader lock acquired")

                try:
                    while True:
                        await _run_once(
                            marketplace_db=marketplace_db,
                            redis_client=redis_client,
                            redis_publisher=redis_publisher,
                            config=config,
                            runtime_state=runtime_state,
                            executor=executor,
                        )
                        await asyncio.sleep(config.poll_seconds)
                finally:
                    try:
                        await conn.fetchval(
                            "SELECT pg_advisory_unlock($1)",
                            lock_key,
                        )
                    except Exception:
                        pass
                    logger.warning("Allocator leader lock released")

        except asyncio.CancelledError:
            logger.info("Order allocator stopped")
            raise
        except Exception as e:
            logger.error("Allocator loop error: %s", e)
            await asyncio.sleep(config.poll_seconds)
