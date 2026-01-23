"""Shared database and Redis connection factories."""

from src.storage.marketplace_db import MarketplaceDB
from src.utils.redis_client import RedisClient, RedisPublisher
from src.utils.logger import get_logger

logger = get_logger(__name__)


async def create_marketplace_db() -> MarketplaceDB:
    """Initialize and return MarketplaceDB instance."""
    db = MarketplaceDB()
    await db.init()
    logger.info("MarketplaceDB initialized")
    return db


async def create_redis_client() -> RedisClient:
    """Initialize and return RedisClient instance."""
    client = RedisClient()
    await client.connect()
    logger.info("RedisClient connected")
    return client


async def create_redis_publisher() -> RedisPublisher:
    """Initialize and return RedisPublisher instance."""
    publisher = RedisPublisher()
    await publisher.connect()
    logger.info("RedisPublisher connected")
    return publisher
