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


async def tides_rewards_monitor_task(db: StatsDB) -> None:
    """
    Background task to discover Bitcoin rewards for TIDES.

    Monitors a Bitcoin address for new coinbase transactions (mining rewards)
    and stores them in the tides_rewards table with TIDES window snapshots.

    Args:
        db: Database connection
    """
    btc_address = os.environ.get("TIDES_BTC_ADDRESS", "")
    start_date_str = os.environ.get("TIDES_REWARDS_START_DATE", "2025-08-20")
    interval = int(os.environ.get("TIDES_REWARDS_CHECK_INTERVAL", "600"))

    if not btc_address:
        logger.error(
            "TIDES_BTC_ADDRESS not configured, disabling TIDES rewards monitoring"
        )
        return

    try:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    except ValueError:
        logger.error(
            f"Invalid TIDES_REWARDS_START_DATE format: {start_date_str}. Use YYYY-MM-DD"
        )
        return

    client = BlockCypherClient(btc_address)

    logger.info(
        f"Starting TIDES rewards monitoring for {btc_address} "
        f"(from {start_date}, every {interval // 60} minutes)"
    )

    while True:
        try:
            if not db or not db.client:
                logger.warning("Database not available for TIDES rewards processing")
                await asyncio.sleep(300)
                continue

            existing_tx_hashes = await load_existing_tx_hashes(db)
            transactions = await client.get_address_transactions()
            new_rewards_count = 0

            for tx in transactions:
                if (
                    tx.get("tx_input_n") == -1  # Coinbase transaction
                    and tx.get("confirmed")  # Has confirmation date
                    and tx.get("block_height")
                ):
                    try:
                        confirmed_date = datetime.strptime(
                            tx["confirmed"][:10], "%Y-%m-%d"
                        ).date()

                        if confirmed_date < start_date:
                            continue

                    except (ValueError, KeyError) as e:
                        logger.warning(
                            f"Invalid confirmation date in tx {tx.get('tx_hash')}: {e}"
                        )
                        continue

                    tx_hash = tx["tx_hash"]

                    if tx_hash not in existing_tx_hashes:
                        tides_window = await get_tides_window(db)
                        snapshot_json = (
                            json.dumps(tides_window) if tides_window else "{}"
                        )

                        insert_query = """
                        INSERT INTO tides_rewards (
                            tx_hash, block_height, btc_amount, 
                            confirmed_at, discovered_at, tides_window
                        )
                        VALUES (
                            %(tx_hash)s, %(block_height)s, %(btc_amount)s, 
                            %(confirmed_at)s, %(discovered_at)s, %(snapshot)s
                        )
                        """

                        btc_amount = tx["value"] / 100000000
                        confirmed_at = datetime.fromisoformat(
                            tx["confirmed"].replace("Z", "+00:00")
                        )

                        params = {
                            "tx_hash": tx_hash,
                            "block_height": tx["block_height"],
                            "btc_amount": btc_amount,
                            "confirmed_at": confirmed_at,
                            "discovered_at": confirmed_at,
                            "snapshot": snapshot_json,
                        }

                        await db.client.command(insert_query, parameters=params)

                        logger.info(
                            f"Stored TIDES reward: {tx_hash} "
                            f"(Block {tx['block_height']}, {btc_amount:.8f} BTC, {confirmed_date})"
                        )
                        new_rewards_count += 1

            if new_rewards_count > 0:
                logger.info(f"Discovered {new_rewards_count} new TIDES rewards")
            else:
                logger.debug("No new TIDES rewards found")

        except Exception as e:
            logger.error(f"Error in TIDES rewards monitoring: {e}")

        await asyncio.sleep(interval)
