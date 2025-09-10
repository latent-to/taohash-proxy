"""
FastAPI application for TaoHash mining statistics API.

Provides RESTful endpoints for querying mining pool and worker statistics.
"""

import asyncio
import os

from src.rewards_extraction.antpool import AntPoolProvider
from src.rewards_extraction.braiins import BraiinsProvider
from src.storage.db import StatsDB
from src.utils.logger import get_logger

logger = get_logger(__name__)

BRAIINS_API_TOKEN = os.environ.get("BRAIINS_API_TOKEN", "")
BRAIINS_API_URL = os.environ.get("BRAIINS_API_URL", "")
REWARD_CHECK_INTERVAL = int(os.environ.get("REWARD_CHECK_INTERVAL", ""))


async def rewards_monitor_task(db: StatsDB):
    """Bg task to automatically fetch and set daily rewards from configured provider."""
    logger.info("Starting daily rewards loop")

    provider_type = os.environ.get("REWARD_PROVIDER", "braiins").lower()

    provider = None
    if provider_type == "antpool":
        api_key = os.environ.get("ANTPOOL_API_KEY", "")
        api_secret = os.environ.get("ANTPOOL_API_SECRET", "")
        user_id = os.environ.get("ANTPOOL_USER_ID", "")

        if not all([api_key, api_secret, user_id]):
            logger.error("AntPool credentials not configured, disabling rewards loop")
            return

        provider = AntPoolProvider(api_key, api_secret, user_id)
        logger.info("Using AntPool rewards provider")

    else:  # Default to braiins
        if not BRAIINS_API_TOKEN:
            logger.error("BRAIINS_API_TOKEN not configured, disabling rewards loop")
            return

        provider = BraiinsProvider(BRAIINS_API_TOKEN)
        logger.info("Using Braiins rewards provider")

    while True:
        try:
            if not db or not db.client:
                logger.warning("Database not available for reward processing")
                await asyncio.sleep(300)
                continue

            rewards = await provider.fetch_daily_rewards(10)

            for reward_data in rewards:
                reward_date = reward_data["date"]
                reward_amount = reward_data["amount"]

                # Check if reward already exists
                check_query = """
                SELECT amount, paid
                FROM daily_rewards
                WHERE date = %(date)s
                LIMIT 1
                """

                result = await db.client.query(
                    check_query, parameters={"date": reward_date}
                )

                if not result.result_rows:
                    # Check if there's worker activity before inserting
                    activity_query = """
                    SELECT COUNT(DISTINCT worker) as worker_count
                    FROM worker_daily_share_value
                    WHERE date = %(date)s
                    """

                    activity_result = await db.client.query(
                        activity_query, parameters={"date": reward_date}
                    )

                    if (
                        activity_result.result_rows
                        and activity_result.result_rows[0][0] > 0
                    ):
                        # Insert new reward only if workers were active
                        insert_query = """
                        INSERT INTO daily_rewards (date, amount)
                        VALUES (%(date)s, %(amount)s)
                        """

                        await db.client.command(
                            insert_query,
                            parameters={
                                "date": reward_date,
                                "amount": reward_amount,
                            },
                        )

                        logger.info(
                            f"Set reward for {reward_date}: {reward_amount} BTC ({provider_type})"
                        )
                    else:
                        logger.warning(
                            f"No worker activity for {reward_date}, skipping reward"
                        )
                else:
                    logger.debug(
                        f"Reward already exists for {reward_date}, keeping existing value"
                    )

        except Exception as e:
            logger.error(f"Error in reward processing task: {e}")

        await asyncio.sleep(REWARD_CHECK_INTERVAL)
