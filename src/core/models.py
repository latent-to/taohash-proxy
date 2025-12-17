"""
Data models for miner control and order management.

This module contains dataclasses used for managing hashrate marketplace orders
and per-miner pool assignments.
"""

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Order:
    """
    Represents a hashrate marketplace order.

    One order can have multiple miners assigned to it.
    All miners on the same order share the same pool config.

    Attributes:
        order_id: Unique identifier for this order
        host: Stratum pool hostname
        port: Stratum pool port
        user: Pool authentication username (buyer's credentials)
        password: Pool authentication password
        expires_at: Unix timestamp when order expires, None for permanent
        created_at: Unix timestamp when order was created
        assigned_miners: Set of worker:ip keys assigned to this order
    """

    order_id: str
    host: str
    port: int
    user: str
    password: str
    expires_at: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    assigned_miners: set[str] = field(default_factory=set)

    def is_expired(self) -> bool:
        """Check if this order has expired."""
        if self.expires_at is None:
            return False
        return time.time() >= self.expires_at

    def remaining_seconds(self) -> Optional[float]:
        """Get remaining seconds until expiry, or None if permanent."""
        if self.expires_at is None:
            return None
        remaining = self.expires_at - time.time()
        return max(0, remaining)

    def to_pool_config(self) -> dict:
        """Convert to pool config dict for MinerSession."""
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "pass": self.password,
            "label": self.order_id,
        }

    def to_dict(self) -> dict:
        """Convert to dictionary for API responses."""
        return {
            "order_id": self.order_id,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "expires_at": self.expires_at,
            "remaining_seconds": self.remaining_seconds(),
            "created_at": self.created_at,
            "assigned_miners": list(self.assigned_miners),
            "miner_count": len(self.assigned_miners),
        }
