from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from src.marketplace_allocator.types import OrderSnapshot


@dataclass
class SimulatedOrder:
    """Order definition helper for scenarios."""

    order_id: str
    price_btc_eh_day: Decimal
    balance_btc: Decimal
    limit_eh_s: Optional[Decimal] = None
    status: str = "active"
    host: str = "pool.example"
    port: int = 3333
    user: str = "user"
    password: str = "x"
    queue_position_time: Optional[datetime] = None

    def to_snapshot(self, *, now: datetime) -> OrderSnapshot:
        queue_time = self.queue_position_time
        if queue_time is None:
            queue_time = now
        if queue_time.tzinfo is None:
            queue_time = queue_time.replace(tzinfo=timezone.utc)

        return OrderSnapshot(
            order_id=str(self.order_id),
            host=str(self.host),
            port=int(self.port),
            user=str(self.user),
            password=str(self.password),
            price_btc_eh_day=Decimal(str(self.price_btc_eh_day)),
            balance_btc=Decimal(str(self.balance_btc)),
            limit_eh_s=self.limit_eh_s,
            queue_position_time=queue_time,
            status=str(self.status),
        )
