"""Pool statistics queries and operations."""

from typing import Any

from src.storage.db import StatsDB
from src.utils.logger import get_logger

logger = get_logger(__name__)


async def get_pool_stats_for_window(db: StatsDB, window: str) -> dict[str, Any]:
    """Get pool statistics for a specific time window."""
    try:
        if window == "5m":
            query = """
            SELECT 
                count(DISTINCT worker) as active_workers,
                count() as shares,
                count() as accepted,
                0 as rejected,
                sum(pool_difficulty) as total_difficulty,
                sum(actual_difficulty) as share_value,
                sum(pool_difficulty) * 4294967296 / 300 as hashrate
            FROM shares
            WHERE ts > now() - INTERVAL 5 MINUTE
            """
        else:
            view_name = f"pool_stats_{window}"
            query = f"""
            SELECT 
                active_workers,
                shares,
                shares as accepted,
                0 as rejected,
                pool_difficulty_sum as total_difficulty,
                actual_difficulty_sum as share_value,
                hashrate
            FROM {view_name}
            """

        result = await db.client.query(query)

        if result.result_rows and result.result_rows[0]:
            row = result.result_rows[0]
            response = {
                "active_workers": int(row[0] or 0),
                "total_shares": int(row[1] or 0),
                "accepted": int(row[2] or 0),
                "rejected": int(row[3] or 0),
                "total_difficulty": float(row[4] or 0),
                "share_value": float(row[5] or 0),
                "hashrate": float(row[6] or 0) if row[6] else 0,
            }

            return response

    except Exception as e:
        logger.error(f"Error in get_pool_stats_for_window: {e}")

    return {
        "active_workers": 0,
        "total_shares": 0,
        "accepted": 0,
        "rejected": 0,
        "total_difficulty": 0,
        "hashrate": 0,
        "share_value": 0,
    }