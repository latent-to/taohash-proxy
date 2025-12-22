import os
import aiohttp

from src.utils.logger import get_logger

logger = get_logger(__name__)


async def broadcast_new_reward(
    network: str,
    block_height: int,
    reward_amount: float,
    tx_hash: str | None = None,
) -> None:

    discord_url = os.environ.get("DISCORD_WEBHOOK_URL")
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")

    if not discord_url and not (tg_token and tg_chat):
        return

    if network.upper() == "BTC":
        explorer_url = f"https://mempool.space/block/{tx_hash}"
        taohash_explorer_url = f"https://taohash.com/btc/blocks/{tx_hash}"
        embed_color = 0xF7931A  # Bitcoin orange
    elif network.upper() == "BCH":
        explorer_url = f"https://blockchair.com/bitcoin-cash/transaction/{tx_hash}"
        taohash_explorer_url = f"https://taohash.com/bch/blocks/{tx_hash}"
        embed_color = 0x00FF00  # Green
    else:
        # Default fallbacks
        explorer_url = ""
        taohash_explorer_url = ""
        embed_color = 0xF7931A
