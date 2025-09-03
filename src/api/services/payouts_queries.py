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


async def create_batch_payout(
    db: StatsDB,
    payouts: List[Dict[str, Any]],
    bitcoin_tx_hash: str,
    payment_method: str = "bitcoin",
    notes: str = "",
    admin_override: bool = False,
    tides_tx_hash: Optional[str] = None,
    processed_by: str = "admin",
) -> Dict[str, Any]:
    """
    Create batch payout with validation and balance updates.

    Args:
        db: Database connection
        payouts: List of {"worker": str, "btc_amount": float}
        bitcoin_tx_hash: Bitcoin transaction hash
        payment_method: Payment method
        notes: Admin notes
        admin_override: Allow negative balances
        tides_tx_hash: Optional TIDES reward to mark processed
        processed_by: Admin who processed this

    Returns:
        Batch payout result with validation details
    """
    try:
        validation_failures, negative_balance_warnings = await validate_worker_balances(
            db, payouts
        )

        # If validation fails and no admin override, return error
        if validation_failures and not admin_override:
            return {
                "success": False,
                "batch_id": None,
                "total_amount": None,
                "processed_workers": None,
                "admin_override_used": False,
                "error": "Insufficient balances for payout",
                "validation_failures": validation_failures,
                "negative_balance_warnings": negative_balance_warnings,
                "suggestion": "Use admin_override=true to proceed or reduce payout amounts",
            }

        # Gen batch ID
        batch_id = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        total_amount = sum(payout["btc_amount"] for payout in payouts)
        processed_at = datetime.now()

        batch_insert = """
        INSERT INTO payout_batches (
            batch_id, total_amount, user_count, payout_data, payment_method,
            external_reference, notes, processed_at, processed_by, created_at
        ) VALUES (
            %(batch_id)s, %(total_amount)s, %(user_count)s, %(payout_data)s,
            %(payment_method)s, %(external_reference)s, %(notes)s, %(processed_at)s,
            %(processed_by)s, %(created_at)s
        )
        """

        payout_data = {
            payout["worker"]: str(payout["btc_amount"]) for payout in payouts
        }

        batch_params = {
            "batch_id": batch_id,
            "total_amount": total_amount,
            "user_count": len(payouts),
            "payout_data": json.dumps(payout_data),
            "payment_method": payment_method,
            "external_reference": bitcoin_tx_hash,
            "notes": notes,
            "processed_at": processed_at,
            "processed_by": processed_by,
            "created_at": processed_at,
        }

        await db.client.command(batch_insert, parameters=batch_params)

        # Add individual user_payouts records and update balances
        for payout in payouts:
            await create_individual_payout(
                db,
                payout["worker"],
                payout["btc_amount"],
                batch_id,
                bitcoin_tx_hash,
                notes,
            )

        # Mark TIDES reward as processed if provided
        if tides_tx_hash:
            await mark_tides_reward_processed(db, tides_tx_hash)

        logger.info(
            f"Created batch payout {batch_id}: {total_amount} BTC to {len(payouts)} workers"
        )

        return {
            "success": True,
            "batch_id": batch_id,
            "total_amount": total_amount,
            "processed_workers": len(payouts),
            "admin_override_used": admin_override,
            "negative_balance_warnings": negative_balance_warnings
            if admin_override
            else None,
        }

    except Exception as e:
        logger.error(f"Failed to create batch payout: {e}")
        raise
