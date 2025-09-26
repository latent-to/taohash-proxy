"""TIDES rewards monitoring task for automatic Bitcoin reward discovery."""

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import aiohttp
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
)

from src.api.services.tides_queries import get_tides_window
from src.utils.time_normalize import normalize_tides_window_snapshot
from src.storage.db import StatsDB
from src.utils.logger import get_logger

logger = get_logger(__name__)


async def process_tides_reward_earnings(
    db: StatsDB,
    tx_hash: str,
    total_btc_amount: float,
    tides_window: Optional[Dict[str, Any]],
    confirmed_at: datetime,
) -> None:
    """
    Creates individual worker earnings and updates balances.

    Args:
        db: Database connection
        tx_hash: Bitcoin transaction hash
        total_btc_amount: Total BTC amount from the reward
        tides_window: TIDES window data with worker percentages
        confirmed_at: When the reward was confirmed
    """
    try:
        if not tides_window or "workers" not in tides_window:
            logger.warning(
                f"No worker data in TIDES window for {tx_hash}, skipping earnings processing"
            )
            return

        workers = tides_window["workers"]
        if not workers:
            logger.warning(f"Empty workers list in TIDES window for {tx_hash}")
            return

        logger.info(
            f"Processing TIDES earnings for {len(workers)} workers from reward {tx_hash}"
        )

        for worker_data in workers:
            worker_name = worker_data.get("name")
            percentage = worker_data.get("percentage", 0)

            if not worker_name:
                logger.warning(f"Worker missing name in TIDES window: {worker_data}")
                continue

            worker_btc_amount = (total_btc_amount * percentage) / 100.0

            if worker_btc_amount <= 0:
                logger.debug(f"Worker {worker_name} has zero earnings, skipping")
                continue

            earning_id = str(uuid.uuid4())
            metadata = {
                "percentage": percentage,
                "share_value": worker_data.get("share_value", 0),
                "shares": worker_data.get("shares", 0),
                "tides_window_start": tides_window.get("window_start"),
                "tides_window_end": tides_window.get("window_end"),
            }

            earnings_insert = """
            INSERT INTO user_earnings (
                earning_id, worker, btc_amount, earning_type, reference,
                tides_reward_id, metadata, earned_at, created_at
            ) VALUES (
                %(earning_id)s, %(worker)s, %(btc_amount)s, %(earning_type)s,
                %(reference)s, %(tides_reward_id)s, %(metadata)s, %(earned_at)s, %(created_at)s
            )
            """

            earnings_params = {
                "earning_id": earning_id,
                "worker": worker_name,
                "btc_amount": worker_btc_amount,
                "earning_type": "tides",
                "reference": tx_hash,
                "tides_reward_id": tx_hash,
                "metadata": json.dumps(metadata),
                "earned_at": confirmed_at,
                "created_at": datetime.now(timezone.utc),
            }

            await db.client.command(earnings_insert, parameters=earnings_params)

            await update_user_balance(
                db, worker_name, worker_btc_amount, "tides_earnings"
            )

            logger.debug(
                f"Processed earnings for {worker_name}: {worker_btc_amount:.8f} BTC "
                f"({percentage:.2f}% of {total_btc_amount:.8f} BTC)"
            )

        logger.info(
            f"Successfully processed TIDES reward {tx_hash} for {len(workers)} workers"
        )

    except Exception as e:
        logger.error(f"Failed to process TIDES reward earnings for {tx_hash}: {e}")
        raise


async def update_user_balance(
    db: StatsDB,
    worker: str,
    btc_amount: float,
    updated_by: str,
) -> None:
    """
    Update user balance by adding to unpaid_amount and total_earned.
    Uses ClickHouse INSERT with ReplacingMergeTree to handle upserts.
    """
    try:
        current_balance_query = """
        SELECT unpaid_amount, paid_amount, total_earned
        FROM user_rewards
        WHERE worker = %(worker)s
        ORDER BY last_updated DESC
        LIMIT 1
        """

        result = await db.client.query(
            current_balance_query, parameters={"worker": worker}
        )

        if result.result_rows:
            current_unpaid = float(result.result_rows[0][0])
            current_paid = float(result.result_rows[0][1])
            current_total_earned = float(result.result_rows[0][2])
        else:
            current_unpaid = 0.0
            current_paid = 0.0
            current_total_earned = 0.0

        new_unpaid = current_unpaid + btc_amount
        new_total_earned = current_total_earned + btc_amount

        balance_insert = """
        INSERT INTO user_rewards (
            worker, unpaid_amount, paid_amount, total_earned, last_updated, updated_by
        ) VALUES (
            %(worker)s, %(unpaid_amount)s, %(paid_amount)s, %(total_earned)s, %(last_updated)s, %(updated_by)s
        )
        """

        balance_params = {
            "worker": worker,
            "unpaid_amount": new_unpaid,
            "paid_amount": current_paid,
            "total_earned": new_total_earned,
            "last_updated": datetime.now(timezone.utc),
            "updated_by": updated_by,
        }

        await db.client.command(balance_insert, parameters=balance_params)

        logger.debug(
            f"Updated balance for {worker}: +{btc_amount:.8f} BTC (unpaid: {new_unpaid:.8f})"
        )

    except Exception as e:
        logger.error(f"Failed to update balance for {worker}: {e}")
        raise


async def load_existing_tx_hashes(db: StatsDB) -> set:
    """Load all existing tx_hashes from tides_rewards table for duplicate checking."""
    try:
        # Note: Research and see what to do in-case of a tx_hash collision.
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

    @retry(
        stop=stop_after_attempt(10),
        wait=wait_exponential(multiplier=2, min=1, max=10),
        reraise=True,
    )
    async def get_tx_details(self, tx_hash: str) -> Dict:
        """Fetch full transaction details for a tx hash."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.api_base}/txs/{tx_hash}",
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        logger.error(
                            f"BlockCypher TX API HTTP error {response.status} for {tx_hash}"
                        )
                        raise aiohttp.ClientResponseError(
                            request_info=response.request_info,
                            history=response.history,
                            status=response.status,
                        )
        except Exception as e:
            logger.error(f"Failed to fetch tx details for {tx_hash}: {e}")
            raise


class OceanAPIClient:
    """Client for interacting with Ocean.xyz API."""

    def __init__(self, btc_address: str):
        self.btc_address = btc_address
        self.api_base = "https://api.ocean.xyz/v1"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=1, max=5),
        reraise=True,
    )
    async def get_payouts(self) -> Dict:
        """Fetch payout data for the BTC address."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.api_base}/earnpay/{self.btc_address}",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        logger.debug(f"Retrieved Ocean payouts for {self.btc_address}")
                        return data
                    else:
                        logger.error(f"Ocean API HTTP error: {response.status}")
                        raise aiohttp.ClientResponseError(
                            request_info=response.request_info,
                            history=response.history,
                            status=response.status,
                        )
        except Exception as e:
            logger.error(f"Failed to fetch Ocean payouts for {self.btc_address}: {e}")
            raise


async def classify_transaction(
    tx_hash: str,
    ocean_data: Optional[Dict[str, Any]],
    ocean_client: OceanAPIClient,
    blockcypher_client: BlockCypherClient,
) -> Optional[str]:
    """
    Classify transaction as coinbase or pool_payout using Ocean API + BlockCypher fallback.

    Returns:
        'coinbase': Coinbase transaction (mining reward)
        'pool_payout': Pool payout transaction
        None: Regular transaction, should be skipped
    """
    # Step 1: Check Ocean
    if ocean_data is not None:
        try:
            for payout in ocean_data["result"]["payouts"]:
                if payout["on_chain_txid"] == tx_hash:
                    return "coinbase" if payout["is_generation_txn"] else "pool_payout"
        except (KeyError, TypeError) as e:
            logger.warning(
                f"Unexpected Ocean payout payload while classifying {tx_hash}: {e}"
            )
            ocean_data = None  # Fallback to direct API call

    if ocean_data is None:
        try:
            fresh_ocean_data = await ocean_client.get_payouts()
            for payout in fresh_ocean_data["result"]["payouts"]:
                if payout["on_chain_txid"] == tx_hash:
                    return "coinbase" if payout["is_generation_txn"] else "pool_payout"
        except Exception as e:
            logger.warning(f"Ocean API failed for {tx_hash}: {e}")

    # Step 2: Check tx_deets for coinbase
    try:
        tx_details = await blockcypher_client.get_tx_details(tx_hash)
        if tx_details.get("block_index") == 0:
            return "coinbase"
    except Exception as e:
        logger.warning(f"BlockCypher tx details failed for {tx_hash}: {e}")

    # Step 3: Skip unknown transactions
    return None


async def tides_rewards_monitor_task(db: StatsDB) -> None:
    """
    Background task to discover Bitcoin rewards for TIDES.

    Monitors a Bitcoin address for new coinbase transactions (mining rewards)
    and stores them in the tides_rewards table with TIDES window snapshots.

    Args:
        db: Database connection
    """
    btc_address = os.environ.get("TIDES_BTC_ADDRESS", "")
    start_date_str = os.environ.get("TIDES_START_DATE", "2025-09-09")
    interval = int(os.environ.get("TIDES_REWARDS_CHECK_INTERVAL", "600"))
    min_confirmations = int(os.environ.get("TIDES_MIN_CONFIRMATIONS", "3"))

    if not btc_address:
        logger.error(
            "TIDES_BTC_ADDRESS not configured, disabling TIDES rewards monitoring"
        )
        return

    try:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    except ValueError:
        logger.error(
            f"Invalid TIDES_START_DATE format: {start_date_str}. Use YYYY-MM-DD"
        )
        return

    client = BlockCypherClient(btc_address)
    ocean_client = OceanAPIClient(btc_address)

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

            try:
                ocean_data = await ocean_client.get_payouts()
            except Exception as e:
                logger.warning(
                    f"Ocean API snapshot fetch failed prior to classification: {e}"
                )
                ocean_data = None

            for tx in transactions:
                if (
                    tx.get("tx_input_n") == -1  # Address received funds
                    and tx.get("confirmed")  # Has confirmation date
                    and tx.get("block_height")
                ):
                    tx_hash = tx["tx_hash"]

                    if tx_hash in existing_tx_hashes:
                        continue

                    confirmations = tx.get("confirmations", 0)
                    if confirmations < min_confirmations:
                        logger.info(
                            f"Skipping {tx_hash}: only {confirmations} confirmations "
                            f"(need {min_confirmations})"
                        )
                        continue

                    try:
                        confirmed_date = datetime.fromisoformat(
                            tx["confirmed"].replace("Z", "+00:00")
                        ).date()

                        if confirmed_date < start_date:
                            continue

                    except (ValueError, KeyError) as e:
                        logger.warning(
                            f"Invalid confirmation date in tx {tx.get('tx_hash')}: {e}"
                        )
                        continue

                    try:
                        source_type = await classify_transaction(
                            tx_hash, ocean_data, ocean_client, client
                        )
                        if not source_type:
                            logger.debug(f"Skipping non-mining transaction {tx_hash}")
                            continue
                    except Exception as e:
                        logger.error(f"Failed to classify transaction {tx_hash}: {e}")
                        continue

                    tides_window = await get_tides_window(db)
                    normalized_window = (
                        normalize_tides_window_snapshot(tides_window)
                        if tides_window
                        else {}
                    )
                    snapshot_json = json.dumps(normalized_window)

                    insert_query = """
                    INSERT INTO tides_rewards (
                        tx_hash, block_height, btc_amount, 
                        confirmed_at, discovered_at, tides_window, source_type
                    )
                    VALUES (
                        %(tx_hash)s, %(block_height)s, %(btc_amount)s, 
                        %(confirmed_at)s, %(discovered_at)s, %(snapshot)s, %(source_type)s
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
                        "source_type": source_type,
                    }

                    await db.client.command(insert_query, parameters=params)
                    await process_tides_reward_earnings(
                        db, tx_hash, btc_amount, normalized_window, confirmed_at
                    )

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
