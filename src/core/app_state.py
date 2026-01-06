"""
Centralized application state for the mining proxy.

This module provides the AppState dataclass that holds all shared state.
"""

from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING, Set, Dict
import asyncio
import os
import socket

if TYPE_CHECKING:
    from ..monitoring.stats import StatsManager
    from ..storage.db import StatsDB
    from ..storage.marketplace_db import MarketplaceDB
    from ..utils.redis_client import RedisClient
    from .command_listener import CommandListener
    from .miner_session import MinerSession


@dataclass
class AppState:
    """
    Centralized application state for the mining proxy instance.
    """

    # Pool configuration (loaded from TOML)
    config: dict = field(default_factory=dict)

    # Active miner sessions
    active_sessions: Set["MinerSession"] = field(default_factory=set)
    active_sessions_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Stats manager for tracking miner statistics
    stats_manager: Optional["StatsManager"] = None

    # Database connection (ClickHouse) for mining statistics
    stats_db: Optional["StatsDB"] = None

    # Database connection (PostgreSQL) for marketplace orders/assignments
    marketplace_db: Optional["MarketplaceDB"] = None

    # Miner registry for command routing (worker:ip -> MinerSession)
    miner_registry: Dict[str, "MinerSession"] = field(default_factory=dict)
    miner_registry_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Redis client for heartbeat publishing
    redis_client: Optional["RedisClient"] = None

    # Command listener for Redis pub/sub
    command_listener: Optional["CommandListener"] = None

    # Unique proxy instance identifier
    proxy_id: str = field(
        default_factory=lambda: os.getenv("HOSTNAME", socket.gethostname())
    )


