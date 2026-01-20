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
