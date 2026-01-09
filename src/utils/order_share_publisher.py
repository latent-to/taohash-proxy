"""
Share publisher for billing order shares.

Provides a small in-process buffer and background worker that
writes share events to Redis Streams without blocking miner sessions.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .logger import get_logger
from .redis_client import RedisClient

logger = get_logger(__name__)

ORDER_SHARE_PUBLISH_QUEUE_SIZE = 50_000
ORDER_SHARE_PUBLISH_MAX_RETRIES = 3
ORDER_SHARE_PUBLISH_BACKOFF_SECONDS = 0.05
ORDER_SHARE_PUBLISH_MAX_BACKOFF_SECONDS = 1.0
ORDER_SHARE_DROP_LOG_EVERY = 1_00


@dataclass(frozen=True)
class OrderShareEvent:
    """
    Immutable payload for a share event to be published to Redis Streams.
    """
    order_id: str
    difficulty: float
    worker: Optional[str]
    share_timestamp: Optional[datetime]


class OrderSharePublisher:
    """
    Background publisher that buffers share events and writes to Redis Streams.

    - Never block the miner session hot path.
    - Retry transient Redis issues with small backoff.
    - Drop on overflow to keep latency predictable.
    """

    def __init__(
        self,
        redis_client: RedisClient,
        queue_size: int = ORDER_SHARE_PUBLISH_QUEUE_SIZE,
        max_retries: int = ORDER_SHARE_PUBLISH_MAX_RETRIES,
        backoff_seconds: float = ORDER_SHARE_PUBLISH_BACKOFF_SECONDS,
    ) -> None:
        """
        Initialize the publisher.

        Args:
            redis_client: Redis client used for stream publishing.
            queue_size: Max buffered events before dropping.
            max_retries: Publish attempts per event.
            backoff_seconds: Initial backoff between retries.
        """
        self.redis_client = redis_client
        self.queue: asyncio.Queue[OrderShareEvent] = asyncio.Queue(maxsize=queue_size)
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._dropped = 0

    def start(self) -> asyncio.Task:
        """
        Start the background publish loop.

        Returns:
            Task representing the background worker.
        """

        if self._task and not self._task.done():
            return self._task
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Order share publisher started (queue=%s, retries=%s)",
            self.queue.maxsize,
            self.max_retries,
        )
        return self._task

    async def stop(self) -> None:
        """
        Stop the background publish loop.
        """
        self._running = False
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    def enqueue_share(
        self,
        order_id: str,
        difficulty: float,
        worker: Optional[str],
        share_timestamp: Optional[datetime],
    ) -> bool:
        """
        Enqueue a share event without blocking.

        Returns:
            True if queued successfully, False if dropped or stopped.
        """
        if not self._running:
            return False
        try:
            self.queue.put_nowait(
                OrderShareEvent(
                    order_id=order_id,
                    difficulty=difficulty,
                    worker=worker,
                    share_timestamp=share_timestamp,
                )
            )
            return True
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % ORDER_SHARE_DROP_LOG_EVERY == 1:
                logger.warning(
                    "Order share queue full; dropped %s events",
                    self._dropped,
                )
            return False

    async def _publish_with_retry(self, event: OrderShareEvent) -> bool:
        """
        Publish a single event with bounded retries.

        Returns:
            True if published, False if retries exhausted or stopped.
        """
        delay = self.backoff_seconds
        for _ in range(self.max_retries):
            if not self._running:
                return False
            if not await self.redis_client.ensure_connected():
                await asyncio.sleep(delay)
                delay = min(delay * 2, ORDER_SHARE_PUBLISH_MAX_BACKOFF_SECONDS)
                continue
            if await self.redis_client.publish_order_share(
                order_id=event.order_id,
                difficulty=event.difficulty,
                worker=event.worker,
                share_timestamp=event.share_timestamp,
            ):
                return True
            await asyncio.sleep(delay)
            delay = min(delay * 2, ORDER_SHARE_PUBLISH_MAX_BACKOFF_SECONDS)
        return False

    async def _run(self) -> None:
        """Background worker loop that drains the queue."""
        try:
            while self._running:
                event = await self.queue.get()
                try:
                    published = await self._publish_with_retry(event)
                    if not published:
                        self._dropped += 1
                        if self._dropped % ORDER_SHARE_DROP_LOG_EVERY == 1:
                            logger.warning(
                                "Order share publish failed; dropped %s events",
                                self._dropped,
                            )
                finally:
                    self.queue.task_done()
        except asyncio.CancelledError:
            raise
