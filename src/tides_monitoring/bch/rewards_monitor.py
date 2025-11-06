"""BCH-specific TIDES rewards monitoring using CryptoAPIs."""

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any

import aiohttp

from src.api.services.tides_queries import get_tides_window
from src.storage.db import StatsDB
from src.tides_monitoring.btc.rewards_monitor import (
    load_existing_tx_hashes,
    process_tides_reward_earnings,
)
from src.utils.logger import get_logger
from src.utils.time_normalize import normalize_tides_window_snapshot

logger = get_logger(__name__)


CRYPTOAPIS_BASE_URL = (
    "https://rest.cryptoapis.io/addresses-latest/utxo/bitcoin-cash/mainnet"
)
CRYPTOAPIS_TIMEOUT = 15.0
CRYPTOAPIS_TX_LIMIT = 25


class CryptoApiAddressClient:
    """Minimal client for fetching BCH address transactions from CryptoAPIs."""

    def __init__(
        self,
        address: str,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.address = address
        self.api_key = api_key
        self.base_url = base_url or CRYPTOAPIS_BASE_URL
        self.timeout = timeout or CRYPTOAPIS_TIMEOUT

    async def get_transactions(self, limit: int = 25) -> list[dict[str, Any]]:
        url = f"{self.base_url}/{self.address}/transactions"
        params = {"limit": str(limit)}
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    raise RuntimeError(
                        f"CryptoAPIs request failed ({response.status}): {text[:200]}"
                    )
                payload = await response.json()

        return payload.get("data", {}).get("items", [])


def _is_coinbase_transaction(item: dict[str, Any]) -> bool:
    inputs = item.get("inputs") or []
    return any(entry.get("coinbase") for entry in inputs)


def _extract_reward_amount(item: dict[str, Any], address: str) -> float:
    total = 0.0
    for output in item.get("outputs", []):
        for candidate in output.get("addresses", []):
            candidate_norm = candidate.lstrip("bitcoincash:")
            if candidate_norm == address:
                try:
                    total += float(output["value"]["amount"])
                except (KeyError, TypeError, ValueError):
                    continue
                break
    return total


async def tides_rewards_monitor_task_bch(db: StatsDB) -> None:
    """Background task to ingest BCH TIDES rewards via CryptoAPIs."""

    raw_address = os.environ.get("TIDES_BCH_ADDRESS", "").strip()
    address = raw_address.lower().lstrip("bitcoincash:")
    if not address:
        logger.error(
            "TIDES_BCH_ADDRESS not configured, disabling BCH TIDES rewards monitoring"
        )
        return

    api_key = os.environ.get("CRYPTOAPIS_API_KEY", "").strip()
    if not api_key:
        logger.error(
            "CRYPTOAPIS_API_KEY not configured, disabling BCH TIDES rewards monitoring"
        )
        return

    start_date_str = os.environ.get("TIDES_REWARDS_START_DATE", "2025-09-28")
    interval = int(os.environ.get("TIDES_REWARDS_CHECK_INTERVAL", "600"))
    tx_limit = CRYPTOAPIS_TX_LIMIT

    try:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    except ValueError:
        logger.error(
            "Invalid TIDES_REWARDS_START_DATE for BCH monitor (%s). Use YYYY-MM-DD.",
            start_date_str,
        )
        return

    client = CryptoApiAddressClient(address, api_key)
