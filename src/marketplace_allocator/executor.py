"""
TaoHash Marketplace Allocator Executor.

The executor applies a plan by:
  * updating miner assignments in Postgres
  * publishing switch commands to proxies via Redis pub/sub (immediate state)
  * updating allocator runtime state (cooldowns, last_tick)
"""

from __future__ import annotations

from dataclasses import dataclass

from src.api.services.market_assignment_queries import (
    create_assignment,
    delete_assignment,
)
from src.api.services.market_order_queries import update_order_status
from src.marketplace_allocator.runtime_state import AllocatorRuntimeState
from src.marketplace_allocator.types import AllocationPlan, AllocationSnapshot
from src.storage.marketplace_db import MarketplaceDB
from src.utils.logger import get_logger
from src.utils.redis_client import RedisPublisher

logger = get_logger(__name__)


@dataclass
class ExecutionResult:
    moved: int = 0
    failed: int = 0


class PlanExecutor:
    """Apply AllocationPlan to DB + Redis."""

    def __init__(
        self,
        marketplace_db: MarketplaceDB,
        redis_publisher: RedisPublisher,
        runtime_state: AllocatorRuntimeState,
    ) -> None:
        self._db = marketplace_db
        self._publisher = redis_publisher
        self._state = runtime_state

    async def apply(
        self, snapshot: AllocationSnapshot, plan: AllocationPlan
    ) -> ExecutionResult:
        if not self._db or not self._db.pool:
            logger.warning("Allocator executor disabled: marketplace DB unavailable")
            return ExecutionResult(moved=0, failed=len(plan.moves))

        if not self._publisher:
            logger.warning("Allocator executor disabled: Redis publisher unavailable")
            return ExecutionResult(moved=0, failed=len(plan.moves))

        order_map = {o.order_id: o for o in snapshot.orders}
        moved_ips: list[str] = []
        result = ExecutionResult()

        for move in plan.moves:
            try:
                if move.to_order_id is None:
                    # No active assignment
                    await delete_assignment(self._db.pool, move.worker_ip)
                    # Switch back to miner's main pool
                    await self._publisher.publish_switch_default(move.worker_ip)
                    moved_ips.append(move.worker_ip)
                    result.moved += 1
                    continue

                # Move to an order
                order = order_map.get(move.to_order_id)
                if not order:
                    logger.warning(
                        "Allocator planned move to unknown order_id=%s for miner %s",
                        move.to_order_id,
                        move.worker_ip,
                    )
                    result.failed += 1
                    continue

                # Ensure assignment points to this order
                await delete_assignment(self._db.pool, move.worker_ip)
                created = await create_assignment(
                    self._db.pool, move.worker_ip, order.order_id
                )
                if created is None:
                    # Best effort fallback: keep miner on default to avoid ghost mining
                    logger.warning(
                        f"Allocator failed to create assignment for miner {move.worker_ip} -> order {order.order_id}",
                    )
                    await self._publisher.publish_switch_default(move.worker_ip)
                    result.failed += 1
                    continue

                pool_config = {
                    "host": order.host,
                    "port": order.port,
                    "user": order.user,
                    "pass": order.password,
                    "label": order.order_id,
                }
                await self._publisher.publish_switch_pool(move.worker_ip, pool_config)
                moved_ips.append(move.worker_ip)
                result.moved += 1

            except Exception as e:
                logger.error(
                    f"Allocator executor error for miner {move.worker_ip}: {e}"
                )
                result.failed += 1

        # Persist runtime state
        try:
            await self._state.record_miner_moves(moved_ips, snapshot.now)
            await self._state.set_last_tick(snapshot.now)
        except Exception as e:
            logger.error(f"Failed to persist allocator runtime state: {e}")

        return result

    async def pause_orders(self, order_ids: list[str]) -> None:
        """Optional hook: pause orders in DB."""

        if not self._db or not self._db.pool:
            return
        for oid in order_ids:
            try:
                await update_order_status(self._db.pool, oid, "paused")
            except Exception:
                continue
