"""
Braiins Pool rewards extraction implementation.
"""

from datetime import datetime, timezone
from typing import List, Dict
import aiohttp

from ..utils.logger import get_logger

logger = get_logger(__name__)


class BraiinsProvider:
    """Fetch rewards from Braiins Pool API."""

    def __init__(self, api_token: str):
        self.api_token = api_token
        self.api_url = "https://pool.braiins.com/accounts/rewards/json/btc"

    async def fetch_daily_rewards(self, days: int = 10) -> List[Dict]:
        """
        Fetch daily rewards from Braiins Pool.

        Returns:
            List of dicts with keys: date, amount
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-SlushPool-Auth-Token": self.api_token,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.api_url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as response:
                    if response.status != 200:
                        logger.error(f"Braiins API HTTP error: {response.status}")
                        return []

                    data = await response.json()
                    daily_rewards = data.get("btc", {}).get("daily_rewards", [])

                    rewards = []
                    for reward_data in daily_rewards[:days]:
                        unix_timestamp = reward_data.get("date")
                        total_reward = reward_data.get("total_reward")

                        if not unix_timestamp or not total_reward:
                            continue

                        try:
                            # Convert Unix timestamp to date
                            reward_date = datetime.fromtimestamp(
                                unix_timestamp, tz=timezone.utc
                            ).date()

                            reward_amount = float(total_reward)

                            if reward_amount > 0:
                                adjusted_amount = reward_amount / 0.975
                                final_amount = adjusted_amount * 0.98  # 2% fee

                                rewards.append(
                                    {"date": reward_date, "amount": final_amount}
                                )
                                logger.debug(
                                    f"Braiins reward: {reward_date} = {final_amount} BTC "
                                    f"(raw: {reward_amount})"
                                )

                        except (ValueError, TypeError) as e:
                            logger.error(f"Error parsing Braiins reward: {e}")
                            continue

                    logger.info(f"Fetched {len(rewards)} rewards from Braiins")
                    return rewards

        except Exception as e:
            logger.error(f"Braiins API request failed: {e}")
            return []
