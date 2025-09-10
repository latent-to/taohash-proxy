"""Reward queries and operations."""

from datetime import date
from typing import Any, Optional

from src.storage.db import StatsDB
from src.utils.logger import get_logger

logger = get_logger(__name__)


async def get_daily_reward_by_date(
    db: StatsDB, requested_date: date
) -> Optional[dict[str, Any]]:
    """Get daily reward for a specific date. Returns None if not found."""

    reward_query = """
    SELECT amount, paid, payment_proof_url
    FROM daily_rewards
    WHERE date = %(date)s
    LIMIT 1
    """

    params = {"date": requested_date}
    reward_result = await db.client.query(reward_query, parameters=params)

    if reward_result.result_rows and reward_result.result_rows[0]:
        return {
            "amount": float(reward_result.result_rows[0][0])
            if reward_result.result_rows[0][0] is not None
            else None,
            "paid": bool(reward_result.result_rows[0][1])
            if len(reward_result.result_rows[0]) > 1
            else False,
            "payment_proof_url": reward_result.result_rows[0][2]
            if len(reward_result.result_rows[0]) > 2
            else None,
        }
    return None


async def get_all_daily_rewards(db: StatsDB) -> list[dict[str, Any]]:
    """Get all daily reward records."""

    query = """
    SELECT 
        date,
        amount,
        updated_at,
        paid,
        payment_proof_url
    FROM daily_rewards
    ORDER BY date DESC
    """

    result = await db.client.query(query)

    rewards = []
    for row in result.result_rows:
        rewards.append(
            {
                "date": row[0],
                "amount": float(row[1]),
                "updated_at": row[2],
                "paid": bool(row[3]) if len(row) > 3 else False,
                "payment_proof_url": row[4] if len(row) > 4 else "",
            }
        )

    return rewards


async def get_unpaid_daily_rewards(db: StatsDB) -> list[dict[str, Any]]:
    """Get unpaid daily reward records."""
    query = """
    SELECT 
        date,
        amount,
        updated_at
    FROM daily_rewards
    WHERE paid = false
    ORDER BY date ASC
    """

    result = await db.client.query(query)

    unpaid_rewards = []
    for row in result.result_rows:
        unpaid_rewards.append(
            {
                "date": row[0],
                "amount": float(row[1]),
                "updated_at": row[2],
            }
        )

    return unpaid_rewards
