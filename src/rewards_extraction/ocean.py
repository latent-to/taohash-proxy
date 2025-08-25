"""
Ocean Mining rewards extraction implementation using BlockCypher API.
"""

from datetime import datetime, timezone, timedelta
from typing import List, Dict
import aiohttp
from collections import defaultdict

from ..utils.logger import get_logger

logger = get_logger(__name__)


class OceanProvider:
    """Fetch rewards for a BTC address via BlockCypher API."""

    def __init__(self, btc_address: str):
        self.btc_address = btc_address
        self.api_base = "https://api.blockcypher.com/v1/btc/main"

    async def _get_current_block_height(self) -> int:
        """Get current Bitcoin block height."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.api_base}",
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as response:
                    if response.status != 200:
                        logger.error(f"BlockCypher API HTTP error: {response.status}")
                        return 0

                    data = await response.json()
                    height = data.get("height", 0)
                    logger.debug(f"Current block height: {height}")
                    return height

        except Exception as e:
            logger.error(f"Failed to get current block height: {e}")
            return 0

    async def _get_address_transactions(self, after_block: int) -> List[Dict]:
        """Get transactions for address since after_block."""
        url = f"{self.api_base}/addrs/{self.btc_address}/full"
        if after_block > 0:
            url += f"?after={after_block}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status != 200:
                        logger.error(
                            f"BlockCypher address API HTTP error: {response.status}"
                        )
                        return []

                    data = await response.json()
                    transactions = data.get("txs", [])
                    logger.debug(
                        f"Fetched {len(transactions)} transactions for {self.btc_address}"
                    )
                    return transactions

        except Exception as e:
            logger.error(f"Failed to get address transactions: {e}")
            return []

    def _calculate_received_amount(self, transaction: Dict) -> int:
        """Calculate amount received by the address in this transaction (in satoshis)."""
        received = 0

        outputs = transaction.get("outputs", [])
        if not outputs:
            return 0

        for output in outputs:
            addresses = output.get("addresses")
            # Skip outputs with no addresses (OP_RETURN, null-data, etc.)
            if not addresses:
                continue

            if self.btc_address in addresses:
                received += output.get("value", 0)

        return received

    def _parse_received_date(self, received_timestamp: str) -> datetime:
        """
        Parse received timestamp to datetime object in UTC.
        BlockCypher returns ISO format like "2025-08-25T15:07:51.743Z"
        """
        try:
            logger.debug(f"Parsing timestamp: {received_timestamp}")
            dt = datetime.fromisoformat(received_timestamp.replace("Z", "+00:00"))
            logger.debug(f"Parsed timestamp: {dt}")
            return dt.astimezone(timezone.utc)

        except Exception as e:
            logger.error(f"Failed to parse timestamp {received_timestamp}: {e}")
            return None

    async def fetch_daily_rewards(self, days: int = 2) -> List[Dict]:
        """
        Fetch daily rewards from Ocean Mining.

        Args:
            days: Number of days to return (2 for Ocean: today + yesterday)

        Returns:
            List of dicts with keys: date, amount
        """
        if not self.btc_address:
            logger.error("Ocean BTC address not configured")
            return []

        # Get current block height
        current_height = await self._get_current_block_height()
        if current_height == 0:
            return []

        # Calculate 3 days ago (144 blocks per day) - fetching extra to ensure complete data
        blocks_per_day = 144
        blockchain_fetch_days = 3
        target_block = current_height - (blocks_per_day * blockchain_fetch_days)

        logger.info(
            f"Fetching Ocean transactions since block {target_block} (last {blockchain_fetch_days} days, returning {days} days)"
        )

        # Get transactions since target block
        transactions = await self._get_address_transactions(target_block)
        if not transactions:
            logger.warning("No transactions found for Ocean address")
            return []

        # Process and group by received date
        daily_rewards = defaultdict(int)  # date -> satoshis
        processed_count = 0

        cutoff_date = (
            datetime.now(timezone.utc) - timedelta(days=blockchain_fetch_days)
        ).date()

        for transaction in transactions:
            received_amount = self._calculate_received_amount(transaction)

            if received_amount > 0:
                received_timestamp = transaction.get("received", "")
                received_date = self._parse_received_date(received_timestamp)
                date_key = received_date.date()

                if date_key < cutoff_date:
                    continue

                daily_rewards[date_key] += received_amount
                processed_count += 1

                logger.debug(
                    f"Ocean reward: {date_key} += {received_amount / 100_000_000:.8f} BTC "
                    f"(tx: {transaction.get('hash', '')[:8]}...)"
                )

        # Convert to result format and sort by date (newest first)
        result = []
        for date_key, total_satoshis in daily_rewards.items():
            btc_amount = total_satoshis / 100_000_000

            final_amount = btc_amount / 0.98

            result.append({"date": date_key, "amount": final_amount})

        result.sort(key=lambda x: x["date"], reverse=True)
        result = result[:days]

        logger.info(
            f"Ocean provider: processed {processed_count} transactions, "
            f"returning {len(result)} daily rewards"
        )

        return result
