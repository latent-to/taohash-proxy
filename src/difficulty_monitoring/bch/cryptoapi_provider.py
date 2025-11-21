from __future__ import annotations

from typing import Optional

import httpx

from src.utils.logger import get_logger

logger = get_logger(__name__)


class CryptoApiDifficultyProvider:
    """Fetch BCH network difficulty using CryptoAPIs."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        endpoint: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        if api_key is None:
            import os

            api_key = os.environ.get("CRYPTOAPIS_API_KEY", "").strip()

        if not api_key:
            raise ValueError(
                "CryptoAPIs provider requires CRYPTOAPIS_API_KEY environment variable."
            )

        self.api_key = api_key
        self.endpoint = endpoint or (
            "https://rest.cryptoapis.io/blocks/utxo/bitcoin-cash/mainnet/latest/details"
        )
        self.timeout = timeout

    async def get_network_difficulty(self) -> Optional[float]:
        """Return the latest BCH network difficulty or None on failure."""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(self.endpoint, headers=headers)
                response.raise_for_status()
        except Exception as exc:
            logger.error("CryptoAPIs request failed: %s", exc)
            return None

        try:
            payload = response.json()
            difficulty = payload["data"]["item"]["difficulty"]
            difficulty_value = float(difficulty)
        except (KeyError, TypeError, ValueError) as exc:
            logger.error("Unexpected payload from CryptoAPIs: %s", exc)
            return None

        if difficulty_value <= 0:
            logger.error(
                "CryptoAPIs returned non-positive difficulty: %s", difficulty_value
            )
            return None

        logger.info("Fetched BCH difficulty from CryptoAPIs: %.0f", difficulty_value)
        return difficulty_value
