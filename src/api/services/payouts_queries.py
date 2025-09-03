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


async def create_individual_payout(
    db: StatsDB,
    worker: str,
    btc_amount: float,
    batch_id: str,
    bitcoin_tx_hash: str,
    notes: str,
) -> None:
    """Create individual payout record and update worker balance."""
    try:
        payout_id = str(uuid.uuid4())
        paid_at = datetime.now()

        payout_insert = """
        INSERT INTO user_payouts (
            payout_id, worker, btc_amount, payout_batch_id, bitcoin_tx_hash,
            notes, paid_at, created_at
        ) VALUES (
            %(payout_id)s, %(worker)s, %(btc_amount)s, %(payout_batch_id)s,
            %(bitcoin_tx_hash)s, %(notes)s, %(paid_at)s, %(created_at)s
        )
        """

        payout_params = {
            "payout_id": payout_id,
            "worker": worker,
            "btc_amount": btc_amount,
            "payout_batch_id": batch_id,
            "bitcoin_tx_hash": bitcoin_tx_hash,
            "notes": notes,
            "paid_at": paid_at,
            "created_at": paid_at,
        }

        await db.client.command(payout_insert, parameters=payout_params)
        await update_user_balance_for_payout(db, worker, btc_amount)

        logger.debug(f"Created payout {payout_id} for {worker}: {btc_amount} BTC")

    except Exception as e:
        logger.error(f"Failed to create individual payout for {worker}: {e}")
        raise


async def update_user_balance_for_payout(
    db: StatsDB,
    worker: str,
    payout_amount: float,
) -> None:
    """
    Update user balance for payout: decrease unpaid_amount, increase paid_amount.
    """
    try:
        current_balance_query = """
        SELECT unpaid_amount, paid_amount, total_earned
        FROM user_rewards
        WHERE worker = %(worker)s
        ORDER BY last_updated DESC
        LIMIT 1
        """

        result = await db.client.query(
            current_balance_query, parameters={"worker": worker}
        )

        if result.result_rows:
            current_unpaid = float(result.result_rows[0][0])
            current_paid = float(result.result_rows[0][1])
            current_total_earned = float(result.result_rows[0][2])
        else:
            current_unpaid = 0.0
            current_paid = 0.0
            current_total_earned = 0.0

        # Cal new balances
        new_unpaid = current_unpaid - payout_amount  # Decrease unpaid
        new_paid = current_paid + payout_amount  # Increase paid

        balance_insert = """
        INSERT INTO user_rewards (
            worker, unpaid_amount, paid_amount, total_earned, last_updated, updated_by
        ) VALUES (
            %(worker)s, %(unpaid_amount)s, %(paid_amount)s, %(total_earned)s, %(last_updated)s, %(updated_by)s
        )
        """

        balance_params = {
            "worker": worker,
            "unpaid_amount": new_unpaid,
            "paid_amount": new_paid,
            "total_earned": current_total_earned,  # Unchanged as we increment thru earnings
            "last_updated": datetime.now(),
            "updated_by": "batch_payout",
        }

        await db.client.command(balance_insert, parameters=balance_params)

        logger.debug(
            f"Updated payout balance for {worker}: unpaid {current_unpaid:.8f} → {new_unpaid:.8f}, "
            f"paid {current_paid:.8f} → {new_paid:.8f}"
        )

    except Exception as e:
        logger.error(f"Failed to update payout balance for {worker}: {e}")
        raise


async def mark_tides_reward_processed(db: StatsDB, tx_hash: str) -> None:
    """Mark TIDES reward as processed."""
    try:
        update_query = """
        ALTER TABLE tides_rewards
        UPDATE processed = true
        WHERE tx_hash = %(tx_hash)s
        """

        await db.client.command(update_query, parameters={"tx_hash": tx_hash})
        logger.info(f"Marked TIDES reward {tx_hash} as processed")

    except Exception as e:
        logger.error(f"Failed to mark TIDES reward {tx_hash} as processed: {e}")
        raise

