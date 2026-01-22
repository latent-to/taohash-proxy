"""
This program acts as the control plane for the hashrate marketplace.
It runs the allocator rules and planner, and applies the resulting plan by updating
the DB assignments and publishing switch commands to proxies via Redis pub/sub.

The core logic lives in the `src.marketplace_allocator` package.
"""

from __future__ import annotations

import asyncio
import zlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from src.api.services.market_order_queries import get_orders
from src.marketplace_allocator.config import load_config, log_config_changes
from src.marketplace_allocator.executor import PlanExecutor
from src.marketplace_allocator.planner import plan_tick
from src.marketplace_allocator.pool_health import evaluate_pool_health
from src.marketplace_allocator.rules import PlanningInput, default_rules
from src.marketplace_allocator.runtime_state import RuntimeStateStore
from src.marketplace_allocator.telemetry import publish_last_tick
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
    runtime_state: RuntimeStateStore,
    executor: PlanExecutor,
) -> None:
    now = datetime.now(timezone.utc)

    # 1) Snapshot orders
    # TODO: Add data structures for get_orders and use them internally and for api calls
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
    # TODO: Add data structures for get_all_miners_with_stats and use them internally and for api calls
    miners: list[MinerSnapshot] = []
    for worker_ip, stats in miners_stats.items():
        try:
            hashrate_gh = float(stats.get("hashrate_gh") or 0.0)
            rejected_breakdown = stats.get("rejected_breakdown") or {}
            miners.append(
                MinerSnapshot(
                    worker_ip=worker_ip,
                    hashrate_eh_s=gh_to_eh(hashrate_gh),
                    pool_label=str(stats.get("pool_label") or "unknown"),
                    accepted=int(stats.get("accepted") or 0),
                    rejected=int(stats.get("rejected") or 0),
                    rejected_stale=int(rejected_breakdown.get("stale") or 0),
                    rejected_duplicate=int(rejected_breakdown.get("duplicate") or 0),
                    rejected_low_diff=int(rejected_breakdown.get("low_diff") or 0),
                    rejected_other=int(rejected_breakdown.get("other") or 0),
                    last_notify_age_s=(
                        float(stats.get("last_notify_age_s"))
                        if stats.get("last_notify_age_s") is not None
                        else None
                    ),
                    worker_name=str(stats.get("worker_name") or ""),
                )
            )
        except Exception as e:
            logger.error(f"Failed to parse miner snapshot: {e}")
            continue

    # 3) Hashprice
    hashprice_btc_eh_day: Optional[Decimal] = None
    if config.enforce_hashprice_floor:
        hashprice_payload = await redis_client.get_hashprice_cache()
        if hashprice_payload:
            try:
                hashprice_btc_eh_day = _parse_decimal(
                    hashprice_payload.get("btc_eh_day")
                )
            except Exception as e:
                logger.error(f"Failed to parse hashprice: {e}")
                hashprice_btc_eh_day = None

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
    for miner in miners:
        if miner.pool_label not in config.default_labels:
            order_ids.add(str(miner.pool_label))

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
        logger.error(f"Pool health evaluation failed: {e}")
        decision = None

    # 5) Apply rules
    planning_context = PlanningInput(
        snapshot=snapshot, config=config, runtime=runtime_view
    )
    if decision is not None:
        planning_context.order_health = decision.order_health
        # Attach health events for telemetry
        for event in decision.events:
            try:
                data = dict(event.get("data") or {})
                if event.get("order_id"):
                    data.setdefault("order_id", event.get("order_id"))
                planning_context.add_event(
                    event.get("level", "info"),
                    event.get("code", "POOL_HEALTH"),
                    event.get("message", "Pool health event"),
                    **data,
                )
            except Exception as e:
                logger.error(f"Failed to add event to planning context: {e}")
                continue

    for rule in default_rules():
        rule.apply(planning_context)

    # 6) Plan
    plan, diag = plan_tick(planning_context)

    # 7) Execute (DB + Redis) and persist runtime state
    await executor.apply(snapshot, plan)

    # 8) Telemetry
    await publish_last_tick(redis_client, diag)

    # Tick summary log
    if diag.planned_switches:
        logger.info(
            f"Allocator tick: "
            f"miners={diag.miner_count} "
            f"supply_eh_s={diag.supply_eh_s} "
            f"eligible={diag.eligible_order_count} "
            f"hard_drain={diag.hard_drain_order_count} "
            f"moves={diag.planned_switches} "
            f"alpha={diag.alpha:.3f}"
        )


async def order_allocator_loop(
    marketplace_db: MarketplaceDB,
    redis_client: RedisClient,
    redis_publisher: RedisPublisher,
    poll_seconds: int,
) -> None:
    """Run the marketplace allocator control loop.

    Continuously allocates miners to marketplace orders based on defined rules and 
    constraints. Runs every `poll_seconds` and hot reloads config on each iteration.

    Args:
        marketplace_db: Database connection for orders and assignments.
        redis_client: Redis client for miner state and runtime persistence.
        redis_publisher: Redis publisher for proxy switch commands.
        poll_seconds: Polling interval.

    Environment variables:
        See `marketplace_allocator.config.load_config` for full list.
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

    runtime_state = RuntimeStateStore(redis_client)
    executor = PlanExecutor(marketplace_db, redis_publisher, runtime_state)

    config = load_config(poll_seconds)
    lock_key = _allocator_lock_key()
    logger.info(
        f"Order allocator starting (\n"
        f"  poll={config.poll_seconds}s,\n"
        f"  p={config.rebalance_rate:.2f},\n"
        f"  interval={config.interval_seconds}s,\n"
        f"  max_switches={config.max_switches_per_tick}\n"
        f")"
    )

    prev_config: Optional[AllocatorConfig] = None

    while True:
        try:
            async with marketplace_db.pool.acquire() as conn:
                got_lock = await conn.fetchval(
                    "SELECT pg_try_advisory_lock($1)",
                    lock_key,
                )

                if not got_lock:
                    logger.debug("Allocator follower: lock held by another instance")
                    await asyncio.sleep(poll_seconds)
                    continue

                logger.info("Allocator leader lock acquired")

                try:
                    while True:
                        config = load_config(poll_seconds)
                        log_config_changes(prev_config, config)
                        prev_config = config

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
            logger.error(f"Allocator loop error: {e}")
            await asyncio.sleep(poll_seconds)
