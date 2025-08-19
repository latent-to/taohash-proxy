"""
AntPool rewards extraction implementation.
"""

import hmac
import hashlib
import time
from datetime import datetime, timedelta
from typing import List, Dict
import aiohttp

from ..utils.logger import get_logger

logger = get_logger(__name__)


class AntPoolProvider:
    """Fetch rewards from AntPool API."""

    def __init__(self, api_key: str, api_secret: str, user_id: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.user_id = user_id
        self.api_url = "https://antpool.com/api/paymentHistoryV2.htm"

    def _generate_signature(self, nonce: str) -> str:
        """Generate HMAC-SHA256 signature for AntPool API."""
        msg = f"{self.user_id}{self.api_key}{nonce}"
        return (
            hmac.new(self.api_secret.encode(), msg.encode(), hashlib.sha256)
            .hexdigest()
            .upper()
        )
    
    async def fetch_account_overview(self) -> Dict:
        """Fetch account overview from AntPool API."""
        api_url = "https://antpool.com/api/accountOverview.htm"

        nonce = str(int(time.time() * 1000))
        payload = {
            "key": self.api_key,
            "userId": self.user_id,
            "nonce": nonce,
            "signature": self._generate_signature(nonce),
            "coin": "BTC",
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_url, data=payload, timeout=aiohttp.ClientTimeout(total=20)
                ) as response:
                    if response.status != 200:
                        logger.error(f"AntPool API HTTP error: {response.status}")
                        return {}
                    
                    data = await response.json()
                    
                    if data.get("code") != 0:
                        logger.error(f"AntPool API error: {data.get('message')}")
                        return {}
                    
                    return data.get("data", {})
                    
        except Exception as e:
            logger.error(f"AntPool API request failed: {e}")
            return {}

    async def fetch_daily_rewards(self, days: int = 10) -> List[Dict]:
        """
        Fetch earnings history from AntPool.

        Returns:
            List of dicts with keys: date, amount
        """
        nonce = str(int(time.time() * 1000))

        payload = {
            "key": self.api_key,
            "userId": self.user_id,
            "nonce": nonce,
            "coin": "BTC",
            "type": "recv",  # Earnings history
            "pageEnable": "0",  # Get all records at once
            "signature": self._generate_signature(nonce),
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.api_url, data=payload, timeout=aiohttp.ClientTimeout(total=20)
                ) as response:
                    if response.status != 200:
                        logger.error(f"AntPool API HTTP error: {response.status}")
                        return []

                    data = await response.json()

                    if data.get("code") != 0:
                        logger.error(f"AntPool API error: {data.get('message')}")
                        return []

                    rows = data.get("data", {}).get("rows", [])

                    rewards = []
                    for row in rows[:days]:
                        timestamp_str = row.get("timestamp", "")
                        try:
                            # Parse "2025-08-10 00:00:00" format
                            dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                            # "Antpool" way of reporting rewards is 1 day behind
                            reward_date = (dt - timedelta(days=1)).date()

                            pplns_amount = float(row.get("pplnsAmount", 0))

                            if pplns_amount > 0:
                                final_amount = pplns_amount * 0.98  # 2% fee
                                rewards.append(
                                    {"date": reward_date, "amount": final_amount}
                                )
                                logger.debug(
                                    f"AntPool reward: {reward_date} = {final_amount} BTC"
                                )

                        except (ValueError, TypeError) as e:
                            logger.error(
                                f"Error parsing AntPool date {timestamp_str}: {e}"
                            )
                            continue

                    logger.info(f"Fetched {len(rewards)} rewards from AntPool")
                    return rewards

        except Exception as e:
            logger.error(f"AntPool API request failed: {e}")
            return []
