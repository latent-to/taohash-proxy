"""Braiins API client for fetching Bitcoin network difficulty."""

from typing import Optional

import httpx

from src.utils.logger import get_logger

logger = get_logger(__name__)


class BraiinsDifficultyProvider:
    """Fetches Bitcoin network difficulty from Braiins API."""

    def __init__(self):
        self.api_url = "https://learn.braiins.com/api/v1.0/difficulty-stats"
        self.timeout = 30.0

    async def get_network_difficulty(self) -> Optional[float]:
        """
        Fetch current Bitcoin network difficulty.

        Returns:
            Network difficulty as float, or None if failed
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(self.api_url)
                response.raise_for_status()

                data = response.json()
                difficulty = data.get("difficulty")

                if difficulty is None:
                    logger.error("No 'difficulty' field in Braiins API response")
                    return None

                difficulty_float = float(difficulty)

                if difficulty_float <= 0:
                    logger.error(f"Invalid difficulty value: {difficulty_float}")
                    return None

                logger.info(f"Fetched network difficulty: {difficulty_float:,.0f}")
                return difficulty_float

        except httpx.TimeoutException:
            logger.error("Timeout fetching difficulty from Braiins API")
        except Exception as e:
            logger.error(f"Unexpected error fetching difficulty: {e}")

        return None
