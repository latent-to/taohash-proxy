"""Payouts queries and operations."""

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.storage.db import StatsDB
from src.utils.logger import get_logger

logger = get_logger(__name__)


async def validate_worker_balances(
    db: StatsDB,
    payouts: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Validate worker balances for batch payout.

    Args:
        db: Database connection
        payouts: List of {"worker": str, "btc_amount": float}

    Returns:
        Tuple of (validation_failures, negative_balance_warnings)
    """
    validation_failures = []
    negative_balance_warnings = []

    try:
        for payout in payouts:
            worker = payout["worker"]
            requested_amount = payout["btc_amount"]

            # Current balance
            balance_query = """
            SELECT unpaid_amount
            FROM user_rewards
            WHERE worker = %(worker)s
            ORDER BY last_updated DESC
            LIMIT 1
            """

            result = await db.client.query(balance_query, parameters={"worker": worker})

            current_balance = (
                float(result.result_rows[0][0]) if result.result_rows else 0.0
            )
            net_balance = current_balance - requested_amount

            if net_balance < 0:
                validation_failure = {
                    "worker": worker,
                    "current_balance": current_balance,
                    "payout_requested": requested_amount,
                    "net_balance": net_balance,
                }
                validation_failures.append(validation_failure)
                negative_balance_warnings.append(validation_failure)

        return validation_failures, negative_balance_warnings

    except Exception as e:
        logger.error(f"Failed to validate worker balances: {e}")
        raise
