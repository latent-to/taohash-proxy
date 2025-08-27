"""TIDES rewards monitoring task for automatic Bitcoin reward discovery."""

import asyncio
import json
import os
from datetime import datetime
from typing import List, Dict

import aiohttp
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
)

from src.api.services.tides_queries import get_tides_window
from src.storage.db import StatsDB
from src.utils.logger import get_logger

logger = get_logger(__name__)


async def load_existing_tx_hashes(db: StatsDB) -> set:
    """Load all existing tx_hashes from tides_rewards table for duplicate checking."""
    try:
        existing_query = "SELECT tx_hash FROM tides_rewards"
        existing_result = await db.client.query(existing_query)
        tx_hashes = {row[0] for row in existing_result.result_rows}
        logger.debug(f"Loaded {len(tx_hashes)} existing tx_hashes")
        return tx_hashes
    except Exception as e:
        logger.error(f"Failed to load existing tx_hashes: {e}")
        return set()  # Return empty set on error


class BlockCypherClient:
    """Client for interacting with BlockCypher Bitcoin API."""

    def __init__(self, btc_address: str):
        self.btc_address = btc_address
        self.api_base = "https://api.blockcypher.com/v1/btc/main"

    @retry(
        stop=stop_after_attempt(10),
        wait=wait_exponential(multiplier=2, min=1, max=10),
        reraise=True,
    )
    async def get_address_transactions(self) -> List[Dict]:
        """Fetch all transactions for the BTC address."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.api_base}/addrs/{self.btc_address}?limit=1000",
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        txrefs = data.get("txrefs", [])
                        logger.debug(
                            f"Retrieved {len(txrefs)} transactions for {self.btc_address}"
                        )
                        return txrefs
                    else:
                        logger.error(f"BlockCypher API HTTP error: {response.status}")
                        raise aiohttp.ClientResponseError(
                            request_info=response.request_info,
                            history=response.history,
                            status=response.status,
                        )
        except Exception as e:
            logger.error(f"Failed to fetch transactions for {self.btc_address}: {e}")
            raise

