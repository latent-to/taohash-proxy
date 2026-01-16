"""
Hashprice monitoring task to derive BTC/EH/day from raw inputs.
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from typing import Any

import aiohttp

from src.utils.logger import get_logger
from src.utils.redis_client import RedisClient

logger = get_logger(__name__)

HASHPRICE_POLL_SECONDS = int(os.getenv("HASHPRICE_POLL_SECONDS", "300"))
HASHPRICE_REQUEST_TIMEOUT_SECONDS = float(
    os.getenv("HASHPRICE_REQUEST_TIMEOUT_SECONDS", "10")
)

BRAIINS_HASHRATE_STATS_URL = "https://insights.braiins.com/api/v1.0/hashrate-stats"
BRAIINS_DIFFICULTY_URL = "https://learn.braiins.com/api/v1.0/difficulty-stats"
MEMPOOL_PRICE_URL = "https://mempool.space/api/v1/prices"
MEMPOOL_HEIGHT_URL = "https://mempool.space/api/blocks/tip/height"

SECONDS_PER_DAY = Decimal("86400")
DIFF_FACTOR = Decimal("4294967296")
EH_MULTIPLIER = Decimal("1e18")


def _block_subsidy_btc(height: int) -> Decimal:
    halvings = height // 210000
    return Decimal("50") / (Decimal(2) ** halvings)


def _parse_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValueError(f"Invalid decimal value: {value}")


def _normalize_fee_value(value: Any) -> Decimal:
    fee = _parse_decimal(value)
    if fee > Decimal("10"):
        return fee / Decimal("1e8")
    return fee


async def _fetch_json(session: aiohttp.ClientSession, url: str) -> dict:
    async with session.get(url) as resp:
        if resp.status != 200:
            raise ValueError(f"Non-200 response {resp.status} from {url}")
        return await resp.json()


async def _fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url) as resp:
        if resp.status != 200:
            raise ValueError(f"Non-200 response {resp.status} from {url}")
        return await resp.text()


async def _fetch_braiins_avg_fees(session: aiohttp.ClientSession) -> Decimal:
    data = await _fetch_json(session, BRAIINS_HASHRATE_STATS_URL)
    if "avg_fees_per_block" not in data:
        raise ValueError("avg_fees_per_block missing from Braiins response")
    return _normalize_fee_value(data["avg_fees_per_block"])


async def _fetch_braiins_difficulty(session: aiohttp.ClientSession) -> Decimal:
    data = await _fetch_json(session, BRAIINS_DIFFICULTY_URL)
    if "difficulty" not in data:
        raise ValueError("difficulty missing from Braiins response")
    return _parse_decimal(data["difficulty"])


async def _fetch_mempool_price(session: aiohttp.ClientSession) -> Decimal:
    data = await _fetch_json(session, MEMPOOL_PRICE_URL)
    usd = data.get("USD")
    if usd is None:
        raise ValueError("USD price missing from mempool response")
    return _parse_decimal(usd)


async def _fetch_mempool_height(session: aiohttp.ClientSession) -> int:
    data = await _fetch_text(session, MEMPOOL_HEIGHT_URL)
    try:
        return int(data.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid height value: {data}") from exc

