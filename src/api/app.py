"""
FastAPI application for TaoHash mining statistics API.

Provides RESTful endpoints for querying mining pool and worker statistics.
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from src.api.tasks.startup import (
    StartupFlags,
    build_background_coroutines,
    schedule_background_tasks,
)
from src.storage.db import StatsDB
from src.utils.env import env_bool
from src.utils.logger import get_logger

from src.api.auth import verify_token, verify_rewards_token

from src.api.models import (
    HealthResponse,
    PoolStatsResponse,
    RewardRequest,
    WorkersShareValueResponse,
    WorkersStatsResponse,
    WorkersTimerangeResponse,
    TidesConfig,
    GeneralConfig,
    TidesRewardDetails,
    TidesRewardsResponse,
    TidesRewardUpdateRequest,
    CustomTidesRewardRequest,
    CustomTidesRewardResponse,
    TidesWindowCalculateRequest,
    TidesWindowResponse,
    EarningsResponse,
    CreateEarningRequest,
    UpdateEarningRequest,
    EarningOperationResponse,
    BatchPayoutRequest,
    BatchPayoutResponse,
    SinglePayoutRequest,
    SinglePayoutResponse,
    PayoutsResponse,
    BatchPayoutDetails,
    UpdatePayoutRequest,
    PayoutOperationResponse,
    BalanceResponse,
    BalancesResponse,
    UserBalance,
    UpdateBalanceRequest,
    BalanceUpdateResponse,
    RawSharesResponse,
)
from src.api.services.pool_queries import get_pool_stats_for_window
from src.api.services.worker_queries import (
    get_worker_counts,
    get_worker_stats,
    get_worker_timerange_stats,
    get_worker_daily_share_value,
)
from src.api.services.reward_queries import (
    get_daily_reward_by_date,
    get_all_daily_rewards,
    get_unpaid_daily_rewards,
)
from src.api.services.config_queries import (
    get_config,
    update_config,
)
from src.api.services.general_config_queries import (
    get_general_config as get_general_config_data,
    update_general_config as update_general_config_data,
)
from src.api.services.tides_queries import (
    get_tides_window as get_tides_window_data,
    calculate_custom_tides_window,
)
from src.api.services.tides_rewards_queries import (
    get_all_tides_rewards,
    get_tides_reward_by_tx_hash,
    update_tides_reward as update_tides_reward_data,
    create_tides_reward as create_tides_reward_data,
)
from src.api.services.earnings_queries import (
    get_worker_earnings,
    create_manual_earning,
    update_earning,
    delete_earning,
)
from src.api.services.payouts_queries import (
    create_batch_payout as create_batch_payout_data,
    create_single_payout_with_validation,
    get_worker_payouts,
    get_batch_payout_details,
    get_all_payouts as get_all_payouts_data,
    update_individual_payout,
    delete_individual_payout,
)
from src.api.services.balance_queries import (
    get_user_balance,
    get_all_user_balances,
    update_user_balance_manually,
)

logger = get_logger(__name__)

SUPPORTED_COINS = {"btc", "bch"}
ACTIVE_COIN = os.environ.get("COIN", "btc").strip().lower()
if not ACTIVE_COIN:
    ACTIVE_COIN = "btc"
if ACTIVE_COIN not in SUPPORTED_COINS:
    logger.warning(
        "Unsupported COIN '%s'. Defaulting to 'btc'. Please configure a supported coin.",
        ACTIVE_COIN,
    )
    ACTIVE_COIN = "btc"

# Rate limiting
limiter = Limiter(key_func=get_remote_address)

# Controllers configuration
ENABLE_REWARD_POLLING = env_bool("ENABLE_REWARD_POLLING")
ENABLE_DIFFICULTY_MONITORING = env_bool("ENABLE_DIFFICULTY_MONITORING")
ENABLE_TIDES_MONITORING = env_bool("ENABLE_TIDES_MONITORING")
ENABLE_TIDES_REWARDS_MONITORING = env_bool("ENABLE_TIDES_REWARDS_MONITORING")
POOL_FEE = float(os.environ.get("POOL_FEE", ""))
MINIMUM_PAYOUT_THRESHOLD = float(os.environ.get("MINIMUM_PAYOUT_THRESHOLD", ""))
MINIMUM_PAYOUT_THRESHOLD_UNIT = os.environ.get("MINIMUM_PAYOUT_THRESHOLD_UNIT", "BTC")

db: Optional[StatsDB] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle."""
    global db
    db = StatsDB()
    if await db.init():
        logger.info("API connected to ClickHouse successfully")
    else:
        logger.warning("API running without database connection")

    logger.info("API starting with coin configuration: %s", ACTIVE_COIN.upper())

    factories = build_background_coroutines(
        ACTIVE_COIN,
        db,
        StartupFlags(
            rewards=ENABLE_REWARD_POLLING,
            difficulty=ENABLE_DIFFICULTY_MONITORING,
            tides=ENABLE_TIDES_MONITORING,
            tides_rewards=ENABLE_TIDES_REWARDS_MONITORING,
        ),
    )
    background_tasks = schedule_background_tasks(factories)

    yield

    for task in background_tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    if db:
        await db.close()
        logger.info("Database connection closed")


app = FastAPI(
    title="TaoHash Mining API",
    description="API for querying mining pool and worker statistics. Requires authentication via Bearer token.",
    version="1.0.0",
    openapi_tags=[
        {"name": "Health", "description": "Service health checks"},
        {
            "name": "Historical Data",
            "description": "Endpoints that require ClickHouse database",
        },
    ],
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check():
    """Health check endpoint (no auth required)."""
    return {
        "status": "ok",
        "timestamp": int(time.time()),
        "database": "connected" if db and db.client else "disconnected",
    }


# Public API
@app.get("/api/stats/summary", tags=["Historical Data"])
@limiter.limit("10/minute")
async def get_stats_summary(request: Request) -> dict[str, Any]:
    """
    Get pool statistics summary (public endpoint).

    Returns core mining metrics and pool configuration.
    Rate limited to 10 requests per minute.
    No authentication required.

    **Requires ClickHouse database to be running.**
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        stats_5m = await get_pool_stats_for_window(db, "5m")
        stats_60m = await get_pool_stats_for_window(db, "60m")
        stats_24h = await get_pool_stats_for_window(db, "24h")

        return {
            "hash_rate_unit": "Gh/s",
            "hash_rate_5m": stats_5m.get("hashrate", 0) / 1e9
            if stats_5m.get("hashrate", 0)
            else 0,
            "hash_rate_60m": stats_60m.get("hashrate", 0) / 1e9
            if stats_60m.get("hashrate", 0)
            else 0,
            "hash_rate_24h": stats_24h.get("hashrate", 0) / 1e9
            if stats_24h.get("hashrate", 0)
            else 0,
            "shares_5m": stats_5m.get("total_shares", 0),
            "shares_60m": stats_60m.get("total_shares", 0),
            "shares_24h": stats_24h.get("total_shares", 0),
            "shares_value_5m": stats_5m.get("total_difficulty", 0),
            "shares_value_60m": stats_60m.get("total_difficulty", 0),
            "shares_value_24h": stats_24h.get("total_difficulty", 0),
            "pool_fee": POOL_FEE,
            "minimum_payout_threshold": MINIMUM_PAYOUT_THRESHOLD,
            "minimum_payout_threshold_unit": MINIMUM_PAYOUT_THRESHOLD_UNIT,
        }

    except Exception as e:
        logger.error(f"Error fetching stats summary: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get(
    "/api/pool/stats",
    response_model=PoolStatsResponse,
    response_model_exclude_none=True,
    tags=["Historical Data"],
)
@limiter.limit("60/minute")
async def get_pool_stats(
    request: Request, token: str = Depends(verify_token)
) -> dict[str, Any]:
    """
    Get aggregated pool statistics.

    Returns statistics for 5-minute, 60-minute, and 24-hour windows.

    **Requires ClickHouse database to be running.**
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        stats_5m = await get_pool_stats_for_window(db, "5m")
        stats_60m = await get_pool_stats_for_window(db, "60m")
        stats_24h = await get_pool_stats_for_window(db, "24h")

        worker_counts = await get_worker_counts(db)

        response = {
            "pool": "all",
            ACTIVE_COIN: {
                "all_time_reward": "0.00000000",  # TODO
                "hash_rate_unit": "Gh/s",
                "hash_rate_5m": stats_5m.get("hashrate", 0) / 1e9  # Convert to GH/s
                if stats_5m.get("hashrate", 0)
                else 0,
                "hash_rate_60m": stats_60m.get("hashrate", 0) / 1e9
                if stats_60m.get("hashrate", 0)
                else 0,
                "hash_rate_24h": stats_24h.get("hashrate", 0) / 1e9
                if stats_24h.get("hashrate", 0)
                else 0,
                "low_workers": 0,
                "off_workers": worker_counts.get("off_workers", 0),
                "ok_workers": worker_counts.get("ok_workers", 0),
                "dis_workers": 0,
                "current_balance": "0.00000000",  # TODO
                "today_reward": "0.00000000",  # TODO
                "estimated_reward": "0.00000000",  # TODO
                "shares_5m": stats_5m.get("total_shares", 0),
                "shares_60m": stats_60m.get("total_shares", 0),
                "shares_24h": stats_24h.get("total_shares", 0),
                "shares_value_5m": stats_5m.get("total_difficulty", 0),
                "shares_value_60m": stats_60m.get("total_difficulty", 0),
                "shares_value_24h": stats_24h.get("total_difficulty", 0),
            },
        }

        return response

    except Exception as e:
        logger.error(f"Error fetching pool stats: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get(
    "/api/workers/stats",
    response_model=WorkersStatsResponse,
    response_model_exclude_none=True,
    tags=["Historical Data"],
)
@limiter.limit("60/minute")
async def get_workers_stats(
    request: Request,
    token: str = Depends(verify_token),
    worker: Optional[str] = None,
) -> dict[str, Any]:
    """
    Get per-worker statistics.

    Can filter by specific worker if provided.

    Args:
        worker: Optional worker filter

    **Requires ClickHouse database to be running.**
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        workers = await get_worker_stats(db, worker)

        workers_dict = {}
        for w in workers:
            worker_key = w["worker"]
            workers_dict[worker_key] = {
                "state": w.get("state", "ok"),
                "last_share": int(w.get("last_share_ts", 0)),
                "hash_rate_unit": "Gh/s",
                "hash_rate_scoring": 0,
                "hash_rate_5m": w.get("hashrate_5m", 0) / 1e9,
                "hash_rate_60m": w.get("hashrate_60m", 0) / 1e9,
                "hash_rate_24h": w.get("hashrate_24h", 0) / 1e9,
                "shares_5m": w.get("shares_5m", 0),
                "shares_60m": w.get("shares_60m", 0),
                "shares_24h": w.get("shares_24h", 0),
                "share_value_5m": w.get("share_value_5m", 0),
                "share_value_60m": w.get("share_value_60m", 0),
                "share_value_24h": w.get("share_value_24h", 0),
            }

        return {ACTIVE_COIN: {"workers": workers_dict}}

    except Exception as e:
        logger.error(f"Error fetching worker stats: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get(
    "/api/workers/timerange",
    response_model=WorkersTimerangeResponse,
    response_model_exclude_none=True,
    tags=["Historical Data"],
)
@limiter.limit("60/minute")
async def get_workers_timerange(
    request: Request,
    start_time: int,
    end_time: int,
    token: str = Depends(verify_token),
) -> dict[str, Any]:
    """
    Get worker statistics for a custom time range.

    Args:
        start_time: Start time as Unix timestamp
        end_time: End time as Unix timestamp

    Returns worker statistics calculated for the specified time period.

    **Requires ClickHouse database to be running.**
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    if start_time >= end_time:
        raise HTTPException(
            status_code=400, detail="start_time must be before end_time"
        )

    time_diff = end_time - start_time
    if time_diff > 30 * 24 * 3600:  # 30 days max
        raise HTTPException(status_code=400, detail="Time range cannot exceed 30 days")

    try:
        return await get_worker_timerange_stats(db, start_time, end_time)

    except Exception as e:
        logger.error(f"Error fetching workers timerange data: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get(
    "/api/workers/share-value/{date}",
    response_model=WorkersShareValueResponse,
    response_model_exclude_none=True,
    tags=["Historical Data"],
)
@limiter.limit("60/minute")
async def get_workers_share_value(
    request: Request,
    date: str,  # Format: YYYY-MM-DD
    token: str = Depends(verify_token),
) -> dict[str, Any]:
    """
    Get worker share values for a specific UTC date.

    Args:
        date: Date in YYYY-MM-DD format

    Returns:
        Worker statistics for the specified date from ClickHouse.

    The response format is identical to /api/workers/timerange
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        requested_date = datetime.strptime(date, "%Y-%m-%d").date()
        today_utc = datetime.now(timezone.utc).date()

        if requested_date > today_utc:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot query future dates. Today is {today_utc}",
            )

        workers_dict = await get_worker_daily_share_value(db, requested_date)

        reward_data = await get_daily_reward_by_date(db, requested_date)
        btc_amount = None
        paid = None
        payment_proof_url = None
        if reward_data:
            btc_amount = reward_data["amount"]
            paid = reward_data["paid"]
            payment_proof_url = reward_data["payment_proof_url"]

        return {
            ACTIVE_COIN: {
                "workers": workers_dict,
                "btc_amount": btc_amount,
                "paid": paid,
                "payment_proof_url": payment_proof_url,
            }
        }

    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid date format. Use YYYY-MM-DD"
        )
    except Exception as e:
        logger.error(f"Error fetching workers share value for {date}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/rewards/{date}", tags=["Rewards"])
@limiter.limit("60/minute")
async def update_daily_reward(
    request: Request,
    date: str,  # Format: YYYY-MM-DD
    reward_data: RewardRequest,
    token: str = Depends(verify_rewards_token),
) -> dict[str, Any]:
    """
    Create or update daily reward fields.

    Can update any combination of: amount, paid, payment_proof_url
    Only provided fields will be updated.

    Validation rules:
    - Cannot set paid=true without an amount (in request or database)
    - Cannot set payment_proof_url without an amount (in request or database)
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        requested_date = datetime.strptime(date, "%Y-%m-%d").date()

        if not any(
            [
                reward_data.amount is not None,
                reward_data.paid is not None,
                reward_data.payment_proof_url is not None,
            ]
        ):
            raise HTTPException(status_code=400, detail="No fields to update")

        if reward_data.amount is not None and reward_data.amount < 0:
            raise HTTPException(status_code=400, detail="Amount must be positive")

        # Check existing record
        check_query = """
        SELECT amount, paid, payment_proof_url
        FROM daily_rewards
        WHERE date = %(date)s
        LIMIT 1
        """

        result = await db.client.query(check_query, parameters={"date": requested_date})
        existing_record = None
        if result.result_rows:
            existing_record = {
                "amount": float(result.result_rows[0][0])
                if result.result_rows[0][0] is not None
                else None,
                "paid": bool(result.result_rows[0][1])
                if len(result.result_rows[0]) > 1
                else False,
                "payment_proof_url": result.result_rows[0][2]
                if len(result.result_rows[0]) > 2
                else "",
            }

        # Validate
        has_amount = reward_data.amount is not None or (
            existing_record and existing_record.get("amount") is not None
        )

        if reward_data.paid and not has_amount:
            raise HTTPException(
                status_code=400, detail="Cannot mark as paid without an amount"
            )

        if reward_data.payment_proof_url is not None and not has_amount:
            raise HTTPException(
                status_code=400, detail="Cannot set payment proof URL without an amount"
            )

        # Build update fields
        update_fields = []
        params = {"date": requested_date}

        if reward_data.amount is not None:
            update_fields.append("amount = %(amount)s")
            params["amount"] = reward_data.amount

        if reward_data.paid is not None:
            update_fields.append("paid = %(paid)s")
            params["paid"] = reward_data.paid

        if reward_data.payment_proof_url is not None:
            update_fields.append("payment_proof_url = %(url)s")
            params["url"] = reward_data.payment_proof_url

        if existing_record:
            # Update existing record
            update_query = f"""
            ALTER TABLE daily_rewards 
            UPDATE {", ".join(update_fields)}
            WHERE date = %(date)s
            """
            await db.client.command(update_query, parameters=params)
        else:
            # Insert new record
            insert_fields = ["date"]
            insert_values = ["%(date)s"]

            if reward_data.amount is not None:
                insert_fields.append("amount")
                insert_values.append("%(amount)s")

            if reward_data.paid is not None:
                insert_fields.append("paid")
                insert_values.append("%(paid)s")

            if reward_data.payment_proof_url is not None:
                insert_fields.append("payment_proof_url")
                insert_values.append("%(url)s")

            insert_query = f"""
            INSERT INTO daily_rewards ({", ".join(insert_fields)})
            VALUES ({", ".join(insert_values)})
            """
            await db.client.command(insert_query, parameters=params)

        response_data = {
            "success": True,
            "date": date,
            "message": "Reward updated successfully",
        }

        if reward_data.amount is not None:
            response_data["amount"] = reward_data.amount
        if reward_data.paid is not None:
            response_data["paid"] = reward_data.paid
        if reward_data.payment_proof_url is not None:
            response_data["payment_proof_url"] = reward_data.payment_proof_url

        return response_data

    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid date format. Use YYYY-MM-DD"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating reward for {date}: {e}")
        raise HTTPException(status_code=500, detail="Database error")


@app.get("/api/rewards", tags=["Rewards"])
@limiter.limit("60/minute")
async def get_all_rewards(
    request: Request,
    token: str = Depends(verify_token),
) -> dict[str, Any]:
    """
    Get all daily reward records.

    Returns:
        Dictionary with date as key and reward info as value
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        reward_records = await get_all_daily_rewards(db)

        rewards = {}
        for record in reward_records:
            date_str = record["date"].strftime("%Y-%m-%d")
            rewards[date_str] = {
                "amount": record["amount"],
                "updated_at": record["updated_at"].isoformat()
                if record["updated_at"]
                else None,
                "paid": record["paid"],
                "payment_proof_url": record["payment_proof_url"],
            }

        return {"success": True, "total_records": len(rewards), "rewards": rewards}

    except Exception as e:
        logger.error(f"Error fetching rewards: {e}")
        raise HTTPException(status_code=500, detail="Database error")


@app.get("/api/shares/raw", response_model=RawSharesResponse, tags=["Historical Data"])
@limiter.limit("60/minute")
async def get_raw_shares(
    request: Request,
    start_time: int,
    end_time: int,
    token: str = Depends(verify_token),
) -> dict[str, Any]:
    """
    Get raw shares data for a custom time range.

    Args:
        start_time: Start time as Unix timestamp
        end_time: End time as Unix timestamp

    Returns raw share records
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    if start_time >= end_time:
        raise HTTPException(
            status_code=400, detail="start_time must be before end_time"
        )
    try:
        start_dt = datetime.fromtimestamp(start_time)
        end_dt = datetime.fromtimestamp(end_time)

        query = """
        SELECT
            toUnixTimestamp(ts) as timestamp,
            miner,
            worker,
            pool,
            pool_difficulty,
            actual_difficulty,
            block_hash,
            pool_requested_difficulty,
            pool_label
        FROM shares
        WHERE ts >= %(start_time_dt)s AND ts < %(end_time_dt)s
        ORDER BY ts
        """

        params = {
            "start_time_dt": start_dt,
            "end_time_dt": end_dt,
        }

        result = await db.client.query(query, parameters=params)

        shares = []
        for row in result.result_rows:
            shares.append(
                {
                    "timestamp": int(row[0]),
                    "miner": row[1],
                    "worker": row[2],
                    "pool": row[3],
                    "pool_difficulty": float(row[4]),
                    "actual_difficulty": float(row[5]),
                    "block_hash": row[6],
                    "pool_requested_difficulty": float(row[7]),
                    "pool_label": row[8],
                }
            )

        return {
            "shares": shares,
            "total_count": len(shares),
            "start_time": start_time,
            "end_time": end_time,
        }

    except Exception as e:
        logger.error(f"Error fetching raw shares data: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/rewards/unpaid", tags=["Rewards"])
@limiter.limit("60/minute")
async def get_unpaid_rewards(
    request: Request,
    token: str = Depends(verify_rewards_token),
) -> dict[str, Any]:
    """
    Get all unpaid reward records.

    Returns:
        List of unpaid rewards with dates and amounts
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        unpaid_records = await get_unpaid_daily_rewards(db)

        unpaid_rewards = []
        total_unpaid = 0.0

        for record in unpaid_records:
            date_str = record["date"].strftime("%Y-%m-%d")
            amount = record["amount"]
            unpaid_rewards.append(
                {
                    "date": date_str,
                    "amount": amount,
                    "updated_at": record["updated_at"].isoformat()
                    if record["updated_at"]
                    else None,
                }
            )
            total_unpaid += amount

        return {
            "success": True,
            "total_records": len(unpaid_rewards),
            "total_unpaid_amount": total_unpaid,
            "unpaid_rewards": unpaid_rewards,
        }

    except Exception as e:
        logger.error(f"Error fetching unpaid rewards: {e}")
        raise HTTPException(status_code=500, detail="Database error")


@app.get(
    "/config/tides",
    tags=["Configuration"],
    summary="Get TIDES Configuration",
    response_description="Current TIDES configuration values",
)
async def get_tides_config(
    request: Request,
    token: str = Depends(verify_rewards_token),
) -> dict[str, Any]:
    """
    Retrieves the current TIDES configuration.

    Returns the current multiplier, network_difficulty, and last updated timestamp.

    ### Sample Request (GET):
    ```bash
    curl -X GET "http://127.0.0.1:8888/config/tides" -H "Authorization: Bearer YOUR_ADMIN_TOKEN"
    ```

    ### Sample Response (200 OK):
    ```json
    {
      "status": "success",
      "config": {
        "multiplier": 8.5,
        "network_difficulty": 400000000.0,
        "updated_at": "2025-01-15T10:30:00"
      }
    }
    ```
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        config_data = await get_config(db)

        if not config_data:
            raise HTTPException(
                status_code=404,
                detail="TIDES configuration not found. Please initialize with a PUT request.",
            )

        return {
            "status": "success",
            "config": config_data,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get TIDES config: {e}")
        raise HTTPException(
            status_code=500,
            detail="An error occurred while retrieving the configuration.",
        )


@app.put(
    "/config/tides",
    tags=["Configuration"],
    summary="Update TIDES Configuration (Partial Updates)",
    response_description="Confirmation of the configuration update.",
)
async def update_tides_config(
    config: TidesConfig,
    request: Request,
    token: str = Depends(verify_rewards_token),
) -> dict[str, Any]:
    """
    Updates the global configuration for the TIDES payout system.

    Can update any combination of: network_difficulty, multiplier
    Only provided fields will be updated.

    ---

    ### Sample Request (partial update):
    ```bash
    curl -X PUT "http://127.0.0.1:8000/config/tides" \\
    -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \\
    -H "Content-Type: application/json" \\
    -d '{
      "network_difficulty": 400000000.0
    }'
    ```

    ### Successful Response (200 OK):
    ```json
    {
      "status": "success",
      "message": "TIDES configuration updated successfully.",
      "updated_fields": {
        "network_difficulty": 400000000.0
      }
    }
    ```
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        if not any(
            [
                config.network_difficulty is not None,
                config.multiplier is not None,
            ]
        ):
            raise HTTPException(status_code=400, detail="No fields to update")

        updated_fields = await update_config(
            db,
            network_difficulty=config.network_difficulty,
            multiplier=config.multiplier,
        )

        return {
            "status": "success",
            "message": "TIDES configuration updated successfully.",
            "updated_fields": updated_fields,
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update TIDES config: {e}")
        raise HTTPException(
            status_code=500,
            detail="An error occurred while updating the configuration.",
        )


@app.get(
    "/config/general",
    tags=["Configuration"],
    summary="Get General Configuration",
    response_description="Current general configuration values",
)
async def get_general_config(
    request: Request,
    token: str = Depends(verify_rewards_token),
) -> dict[str, Any]:
    """
    Retrieves the current general configuration.

    Returns the current worker_percentage and last updated timestamp.

    ### Sample Request (GET):
    ```bash
    curl -X GET "http://127.0.0.1:8888/config/general" -H "Authorization: Bearer YOUR_ADMIN_TOKEN"
    ```

    ### Sample Response (200 OK):
    ```json
    {
      "status": "success",
      "config": {
        "worker_percentage": 85.0,
        "updated_at": "2025-01-15T10:30:00"
      }
    }
    ```
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        config_data = await get_general_config_data(db)

        if not config_data:
            raise HTTPException(
                status_code=404,
                detail="General configuration not found. Please initialize with a PUT request.",
            )

        return {
            "status": "success",
            "config": config_data,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get general config: {e}")
        raise HTTPException(
            status_code=500,
            detail="An error occurred while retrieving the configuration.",
        )


@app.put(
    "/config/general",
    tags=["Configuration"],
    summary="Update General Configuration (Partial Updates)",
    response_description="Confirmation of the configuration update.",
)
async def update_general_config(
    config: GeneralConfig,
    request: Request,
    token: str = Depends(verify_rewards_token),
) -> dict[str, Any]:
    """
    Updates the general configuration.

    Can update any combination of: worker_percentage
    Only provided fields will be updated.

    ---

    ### Sample Request (partial update):
    ```bash
    curl -X PUT "http://127.0.0.1:8000/config/general" \\
    -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \\
    -H "Content-Type: application/json" \\
    -d '{
      "worker_percentage": 85.0
    }'
    ```

    ### Successful Response (200 OK):
    ```json
    {
      "status": "success",
      "message": "General configuration updated successfully.",
      "updated_fields": {
        "worker_percentage": 85.0
      }
    }
    ```
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        if not any(
            [
                config.worker_percentage is not None,
            ]
        ):
            raise HTTPException(status_code=400, detail="No fields to update")

        updated_fields = await update_general_config_data(
            db,
            worker_percentage=config.worker_percentage,
        )

        return {
            "status": "success",
            "message": "General configuration updated successfully.",
            "updated_fields": updated_fields,
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update general config: {e}")
        raise HTTPException(
            status_code=500,
            detail="An error occurred while updating the configuration.",
        )


@app.get(
    "/api/tides/window",
    tags=["TIDES"],
    summary="Get TIDES Window",
    response_description="Current TIDES sliding window data",
    response_model=TidesWindowResponse,
)
async def get_tides_window(
    request: Request,
    token: str = Depends(verify_token),
) -> dict[str, Any]:
    """
    Get current TIDES sliding window data.

    Returns worker statistics for the current TIDES difficulty window,
    including worker shares, percentages, and window metadata.

    ### Sample Response (200 OK):
    ```json
    {
      "workers": [
        {
          "name": "worker1",
          "shares": 1000,
          "share_value": 50000000000.0,
          "percentage": 25.5
        }
      ],
      "share_log_window": 722500000000000.0,
      "network_difficulty": 85000000000000.0,
      "multiplier": 8.5,
      "window_start": "2025-01-10T15:30:00",
      "window_end": "2025-01-15T10:30:00",
      "total_difficulty_in_window": 722500000000000.0,
      "total_workers": 42,
      "updated_at": "2025-01-15T10:30:00"
    }
    ```
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        tides_data = await get_tides_window_data(db)

        if not tides_data:
            raise HTTPException(
                status_code=404,
                detail="TIDES window data not available. Please wait for initial calculation.",
            )

        return {
            "status": "success",
            "data": tides_data,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching TIDES window: {e}")
        raise HTTPException(
            status_code=500,
            detail="An error occurred while retrieving TIDES data.",
        )


@app.get(
    "/api/tides/rewards",
    response_model=TidesRewardsResponse,
    tags=["TIDES"],
    summary="Get TIDES Rewards Summary",
    response_description="List of all discovered TIDES rewards",
)
@limiter.limit("60/minute")
async def get_tides_rewards(
    request: Request,
    token: str = Depends(verify_token),
) -> dict[str, Any]:
    """
    Get summary of all TIDES rewards.

    Returns a list of all discovered Bitcoin rewards for TIDES with essential
    information: transaction hash, BTC amount, confirmation date, and processing status.
    Results are ordered by confirmation date (newest first).

    ### Sample Request:
    ```bash
    curl -X GET "http://127.0.0.1:8888/api/tides/rewards" \
         -H "Authorization: Bearer YOUR_TOKEN"
    ```
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        rewards = await get_all_tides_rewards(db)
        return {"rewards": rewards}

    except Exception as e:
        logger.error(f"Error fetching TIDES rewards: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get(
    "/api/tides/rewards/{tx_hash}",
    response_model=TidesRewardDetails,
    tags=["TIDES"],
    summary="Get TIDES Reward Details",
    response_description="Full details for a specific TIDES reward",
)
@limiter.limit("60/minute")
async def get_tides_reward_details(
    request: Request,
    tx_hash: str,
    token: str = Depends(verify_token),
) -> dict[str, Any]:
    """
    Get full details for a specific TIDES reward by transaction hash.

    Returns complete reward information including the TIDES window snapshot
    that was captured when the reward was discovered.

    Args:
        tx_hash: Bitcoin transaction hash (64 character hex string)

    ### Sample Request:
    ```bash
    curl -X GET "http://127.0.0.1:8888/api/tides/rewards/abc123..." \
         -H "Authorization: Bearer YOUR_TOKEN"
    ```
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        reward = await get_tides_reward_by_tx_hash(db, tx_hash)

        if not reward:
            raise HTTPException(status_code=404, detail="TIDES reward not found")

        return reward

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching TIDES reward {tx_hash}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.put(
    "/api/tides/rewards/{tx_hash}",
    tags=["TIDES"],
    summary="Update TIDES Reward",
    response_description="Confirmation of the reward update",
)
@limiter.limit("60/minute")
async def update_tides_reward(
    request: Request,
    tx_hash: str,
    reward_data: TidesRewardUpdateRequest,
    token: str = Depends(verify_rewards_token),
) -> dict[str, Any]:
    """
    Update TIDES reward fields (btc_amount and/or processed status).

    Can update any combination of: btc_amount, processed
    Only provided fields will be updated. Other fields remain unchanged.

    Args:
        tx_hash: Bitcoin transaction hash to update
        reward_data: Fields to update

    ### Sample Request:
    ```bash
    curl -X PUT "http://127.0.0.1:8888/api/tides/rewards/abc123..." \
         -H "Authorization: Bearer YOUR_REWARDS_TOKEN" \
         -H "Content-Type: application/json" \
         -d '{"btc_amount": 6.25, "processed": true}'
    ```
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        if not any(
            [
                reward_data.btc_amount is not None,
                reward_data.processed is not None,
            ]
        ):
            raise HTTPException(status_code=400, detail="No fields to update")

        updated_fields = await update_tides_reward_data(
            db,
            tx_hash=tx_hash,
            btc_amount=reward_data.btc_amount,
            processed=reward_data.processed,
        )

        if updated_fields is None:
            raise HTTPException(status_code=404, detail="TIDES reward not found")

        return {
            "success": True,
            "tx_hash": tx_hash,
            "message": "TIDES reward updated successfully",
            "updated_fields": updated_fields,
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating TIDES reward {tx_hash}: {e}")
        raise HTTPException(status_code=500, detail="Database error")


@app.post(
    "/api/tides/rewards",
    response_model=CustomTidesRewardResponse,
    tags=["TIDES"],
    summary="Create TIDES Reward",
    response_description="The created TIDES reward with calculated window",
)
@limiter.limit("60/minute")
async def create_tides_reward(
    request: Request,
    reward_data: CustomTidesRewardRequest,
    token: str = Depends(verify_rewards_token),
) -> dict[str, Any]:
    """
    Create a new TIDES reward with automatically calculated window snapshot.
    
    Calculates the TIDES window at the specified confirmed_at datetime and
    stores the reward with the window data for historical analysis.

    Args:
        reward_data: TIDES reward data to create

    ### Sample Request:
    ```bash
    curl -X POST "http://127.0.0.1:8888/api/tides/rewards" \
         -H "Authorization: Bearer YOUR_REWARDS_TOKEN" \
         -H "Content-Type: application/json" \
         -d '{
           "tx_hash": "abc123...",
           "block_height": 850000,
           "btc_amount": 3.125,
           "confirmed_at": "2024-08-15T14:30:00Z"
         }'
    ```
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        reward = await create_tides_reward_data(
            db,
            tx_hash=reward_data.tx_hash,
            block_height=reward_data.block_height,
            btc_amount=reward_data.btc_amount,
            confirmed_at=reward_data.confirmed_at,
        )

        return reward

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating TIDES reward {reward_data.tx_hash}: {e}")
        raise HTTPException(status_code=500, detail="Database error")


@app.post(
    "/api/tides/window/calculate",
    tags=["TIDES"],
    summary="Calculate TIDES Window at Custom Time",
    response_description="TIDES window calculated from specified datetime",
)
@limiter.limit("10/minute")
async def calculate_tides_window(
    request: Request,
    calculate_request: TidesWindowCalculateRequest,
    token: str = Depends(verify_token),
) -> dict[str, Any]:
    """
    Calculate TIDES window from a specific datetime (without storing).

    Calculates the TIDES window backwards from the specified datetime:
    SPECIFIED_TIME → Remaining Day (partial) → Full Days → Start Date (partial) → Target Reached

    ### Sample Request:
    ```bash
    curl -X POST "http://127.0.0.1:8888/api/tides/window/calculate" \
         -H "Authorization: Bearer YOUR_TOKEN" \
         -H "Content-Type: application/json" \
         -d '{"end_datetime": "2025-08-27T15:30:00Z"}'
    ```
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        end_dt = calculate_request.end_datetime
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        else:
            end_dt = end_dt.astimezone(timezone.utc)

        window_data = await calculate_custom_tides_window(db, end_dt)

        return {
            "status": "success",
            "data": window_data,
        }

    except Exception as e:
        logger.error(
            f"Error calculating TIDES window at {calculate_request.end_datetime}: {e}"
        )
        raise HTTPException(
            status_code=500,
            detail="An error occurred while calculating the TIDES window.",
        )


@app.get("/api/earnings/{worker}", response_model=EarningsResponse, tags=["Earnings"])
@limiter.limit("60/minute")
async def get_earnings(
    request: Request,
    worker: str,
    limit: int = 100,
    offset: int = 0,
    earning_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    token: str = Depends(verify_token),
) -> EarningsResponse:
    """
    Get earnings for a specific worker with optional filters.

    Query parameters:
    - limit: Maximum number of results (default 100)
    - offset: Number of results to skip (default 0)
    - earning_type: Filter by earning type ('tides', 'pplns', 'manual')
    - date_from: Filter earnings from this date (YYYY-MM-DD)
    - date_to: Filter earnings to this date (YYYY-MM-DD)
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        parsed_date_from = None
        parsed_date_to = None

        if date_from:
            try:
                parsed_date_from = datetime.strptime(date_from, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="Invalid date_from format. Use YYYY-MM-DD"
                )

        if date_to:
            try:
                parsed_date_to = datetime.strptime(date_to, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="Invalid date_to format. Use YYYY-MM-DD"
                )

        earnings = await get_worker_earnings(
            db, worker, limit, offset, earning_type, parsed_date_from, parsed_date_to
        )

        return EarningsResponse(
            earnings=earnings,
            total_count=None,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting earnings for worker {worker}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/earnings", response_model=EarningOperationResponse, tags=["Earnings"])
@limiter.limit("30/minute")
async def create_earning(
    request: Request,
    earning_request: CreateEarningRequest,
    token: str = Depends(verify_rewards_token),
) -> EarningOperationResponse:
    """
    Create a manual earning record.
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        earning = await create_manual_earning(
            db,
            earning_request.worker,
            earning_request.btc_amount,
            earning_request.earning_type,
            earning_request.reference,
            earning_request.metadata,
            earning_request.earned_at,
        )

        return EarningOperationResponse(
            success=True,
            earning_id=earning["earning_id"],
            message=f"Created earning for {earning_request.worker}: {earning_request.btc_amount} BTC",
        )

    except Exception as e:
        logger.error(f"Error creating earning for {earning_request.worker}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.put(
    "/api/earnings/{earning_id}",
    response_model=EarningOperationResponse,
    tags=["Earnings"],
)
@limiter.limit("30/minute")
async def update_earning_record(
    request: Request,
    earning_id: str,
    update_request: UpdateEarningRequest,
    token: str = Depends(verify_rewards_token),
) -> EarningOperationResponse:
    """
    Update an existing earning record.
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        if not any(
            [
                update_request.btc_amount is not None,
                update_request.earning_type is not None,
                update_request.metadata is not None,
                update_request.reference is not None,
            ]
        ):
            raise HTTPException(
                status_code=400, detail="At least one field must be provided for update"
            )

        earning = await update_earning(
            db,
            earning_id,
            update_request.btc_amount,
            update_request.earning_type,
            update_request.metadata,
            update_request.reference,
        )

        if not earning:
            raise HTTPException(status_code=404, detail="Earning not found")

        return EarningOperationResponse(
            success=True, earning_id=earning_id, message=f"Updated earning {earning_id}"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating earning {earning_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.delete(
    "/api/earnings/{earning_id}",
    response_model=EarningOperationResponse,
    tags=["Earnings"],
)
@limiter.limit("30/minute")
async def delete_earning_record(
    request: Request,
    earning_id: str,
    token: str = Depends(verify_rewards_token),
) -> EarningOperationResponse:
    """
    Delete an earning record and update balances.
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        success = await delete_earning(db, earning_id)

        if not success:
            raise HTTPException(status_code=404, detail="Earning not found")

        return EarningOperationResponse(
            success=True, earning_id=earning_id, message=f"Deleted earning {earning_id}"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting earning {earning_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/payouts/batch", response_model=BatchPayoutResponse, tags=["Payouts"])
@limiter.limit("10/minute")
async def create_batch_payout(
    request: Request,
    payout_request: BatchPayoutRequest,
    token: str = Depends(verify_rewards_token),
) -> BatchPayoutResponse:
    """
    Create a batch payout to multiple workers.

    Validates worker balances before processing. If any worker would have
    insufficient balance, returns error unless admin_override=true.

    Request body:
    {
        "payouts": [{"worker": "worker1", "btc_amount": 0.001}],
        "bitcoin_tx_hash": "abc123...",
        "payment_method": "bitcoin",
        "notes": "Weekly payout",
        "admin_override": false,
        "tides_tx_hashes": ["hash1", "hash2"],
    }
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        if not payout_request.payouts:
            raise HTTPException(
                status_code=400, detail="At least one payout must be provided"
            )

        payouts_data = [
            {"worker": item.worker, "btc_amount": item.btc_amount}
            for item in payout_request.payouts
        ]

        result = await create_batch_payout_data(
            db,
            payouts_data,
            payout_request.bitcoin_tx_hash,
            payout_request.payment_method,
            payout_request.notes,
            payout_request.admin_override,
            payout_request.tides_tx_hashes,
            "api_admin",
        )

        if result["success"]:
            return BatchPayoutResponse(
                success=True,
                batch_id=result["batch_id"],
                total_amount=result["total_amount"],
                processed_workers=result["processed_workers"],
                tides_rewards_processed=result.get("tides_rewards_processed"),
                admin_override_used=result.get("admin_override_used"),
                error=None,
                negative_balance_warnings=result.get("negative_balance_warnings"),
                suggestion=None,
            )
        else:
            return BatchPayoutResponse(
                success=False,
                batch_id=None,
                total_amount=None,
                processed_workers=None,
                tides_rewards_processed=None,
                admin_override_used=False,
                error=result["error"],
                negative_balance_warnings=result.get("negative_balance_warnings"),
                suggestion=result.get("suggestion"),
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating batch payout: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/payouts/single", response_model=SinglePayoutResponse, tags=["Payouts"])
@limiter.limit("60/minute")
async def create_single_payout_endpoint(
    request: Request,
    payout_request: SinglePayoutRequest,
    token: str = Depends(verify_rewards_token),
) -> SinglePayoutResponse:
    """
    Create a single worker payout.

    Example request:
    {
        "worker": "worker1",
        "btc_amount": 0.001,
        "bitcoin_tx_hash": "abc123...",
        "payment_method": "bitcoin",
        "notes": "Individual payout",
        "admin_override": false
    }
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        result = await create_single_payout_with_validation(
            db,
            payout_request.worker,
            payout_request.btc_amount,
            payout_request.bitcoin_tx_hash,
            payout_request.payment_method,
            payout_request.notes,
            payout_request.admin_override,
            "api_admin",
        )
        success = bool(result.get("success"))
        return SinglePayoutResponse(
            success=success,
            worker=result.get("worker"),
            btc_amount=result.get("btc_amount"),
            payout_id=result.get("payout_id"),
            bitcoin_tx_hash=result.get("bitcoin_tx_hash"),
            admin_override_used=result.get("admin_override_used"),
            current_balance=result.get("current_balance"),
            net_balance=result.get("net_balance"),
            error=result.get("error"),
            suggestion=result.get("suggestion"),
        )

    except Exception as e:
        logger.error(f"Error creating single payout for {payout_request.worker}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/payouts/{worker}", response_model=PayoutsResponse, tags=["Payouts"])
@limiter.limit("60/minute")
async def get_payouts(
    request: Request,
    worker: str,
    limit: int = 100,
    offset: int = 0,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    token: str = Depends(verify_token),
) -> PayoutsResponse:
    """
    Get payouts for a specific worker with optional filters.

    Query parameters:
    - limit: Maximum number of results (default 100)
    - offset: Number of results to skip (default 0)
    - date_from: Filter payouts from this date (YYYY-MM-DD)
    - date_to: Filter payouts to this date (YYYY-MM-DD)
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        parsed_date_from = None
        parsed_date_to = None

        if date_from:
            try:
                parsed_date_from = datetime.strptime(date_from, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="Invalid date_from format. Use YYYY-MM-DD"
                )

        if date_to:
            try:
                parsed_date_to = datetime.strptime(date_to, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="Invalid date_to format. Use YYYY-MM-DD"
                )

        payouts = await get_worker_payouts(
            db, worker, limit, offset, parsed_date_from, parsed_date_to
        )

        return PayoutsResponse(
            payouts=payouts,
            total_count=None,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting payouts for worker {worker}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get(
    "/api/payouts/batch/{batch_id}", response_model=BatchPayoutDetails, tags=["Payouts"]
)
@limiter.limit("60/minute")
async def get_batch_details(
    request: Request,
    batch_id: str,
    token: str = Depends(verify_rewards_token),
) -> BatchPayoutDetails:
    """
    Get detailed batch payout information including all individual payouts.
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        batch_details = await get_batch_payout_details(db, batch_id)

        if not batch_details:
            raise HTTPException(status_code=404, detail="Batch not found")

        return BatchPayoutDetails(**batch_details)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting batch details for {batch_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/payouts", response_model=PayoutsResponse, tags=["Payouts"])
@limiter.limit("60/minute")
async def get_all_payouts(
    request: Request,
    worker: Optional[str] = None,
    batch_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    token: str = Depends(verify_token),
) -> PayoutsResponse:
    """
    Get payouts with optional filters.

    Query parameters:
    - worker: Filter by worker name
    - batch_id: Filter by batch ID
    - date_from: Filter payouts from this date (YYYY-MM-DD)
    - date_to: Filter payouts to this date (YYYY-MM-DD)
    - limit: Maximum number of results (default 100)
    - offset: Number of results to skip (default 0)
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        parsed_date_from = None
        parsed_date_to = None

        if date_from:
            try:
                parsed_date_from = datetime.strptime(date_from, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="Invalid date_from format. Use YYYY-MM-DD"
                )

        if date_to:
            try:
                parsed_date_to = datetime.strptime(date_to, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="Invalid date_to format. Use YYYY-MM-DD"
                )

        payouts = await get_all_payouts_data(
            db, worker, batch_id, parsed_date_from, parsed_date_to, limit, offset
        )

        return PayoutsResponse(payouts=payouts, total_count=None)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting payouts: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.put(
    "/api/payouts/{payout_id}", response_model=PayoutOperationResponse, tags=["Payouts"]
)
@limiter.limit("30/minute")
async def update_payout(
    request: Request,
    payout_id: str,
    update_request: UpdatePayoutRequest,
    token: str = Depends(verify_rewards_token),
) -> PayoutOperationResponse:
    """
    Update an individual payout record and recalculate balances.
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        if not any(
            [
                update_request.btc_amount is not None,
                update_request.bitcoin_tx_hash is not None,
                update_request.notes is not None,
            ]
        ):
            raise HTTPException(
                status_code=400, detail="At least one field must be provided for update"
            )

        payout = await update_individual_payout(
            db,
            payout_id,
            update_request.btc_amount,
            update_request.bitcoin_tx_hash,
            update_request.notes,
        )

        if not payout:
            raise HTTPException(status_code=404, detail="Payout not found")

        return PayoutOperationResponse(
            success=True, payout_id=payout_id, message=f"Updated payout {payout_id}"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating payout {payout_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.delete(
    "/api/payouts/{payout_id}", response_model=PayoutOperationResponse, tags=["Payouts"]
)
@limiter.limit("30/minute")
async def delete_payout(
    request: Request,
    payout_id: str,
    token: str = Depends(verify_rewards_token),
) -> PayoutOperationResponse:
    """
    Delete an individual payout record and rollback balances.
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        success = await delete_individual_payout(db, payout_id)

        if not success:
            raise HTTPException(status_code=404, detail="Payout not found")

        return PayoutOperationResponse(
            success=True,
            payout_id=payout_id,
            message=f"Deleted payout {payout_id} and rolled back balances",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting payout {payout_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/balance/{user}", response_model=BalanceResponse, tags=["Balances"])
@limiter.limit("60/minute")
async def get_balance(
    request: Request,
    user: str,
    token: str = Depends(verify_token),
) -> BalanceResponse:
    """
    Get current balance for a specific worker.

    Returns the worker's current unpaid_amount, paid_amount, total_earned
    from the user_rewards table.
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        balance_data = await get_user_balance(db, user)

        if not balance_data:
            raise HTTPException(status_code=404, detail="User balance not found")

        return BalanceResponse(balance=UserBalance(**balance_data))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting balance for worker {user}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/balances", response_model=BalancesResponse, tags=["Balances"])
@limiter.limit("30/minute")
async def get_all_balances(
    request: Request,
    limit: Optional[int] = Query(
        None, description="Maximum number of records to return"
    ),
    offset: Optional[int] = Query(None, description="Number of records to skip"),
    token: str = Depends(verify_token),
) -> BalancesResponse:
    """
    Get current balances for all users.

    Returns all worker balances from the user_rewards table with optional pagination.
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        balances_data = await get_all_user_balances(db, limit, offset)

        balances = [UserBalance(**balance) for balance in balances_data]

        return BalancesResponse(balances=balances)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting all balances: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.put(
    "/api/balance/{user}",
    response_model=BalanceUpdateResponse,
    tags=["Admin", "Balances"],
)
@limiter.limit("30/minute")
async def update_user_balance(
    request: Request,
    user: str,
    balance_request: UpdateBalanceRequest,
    token: str = Depends(verify_rewards_token),
) -> BalanceUpdateResponse:
    """
    Update worker balance directly.

    Can update unpaid_amount, paid_amount, and/or total_earned.
    At least one field must be provided.
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    if all(
        field is None
        for field in [
            balance_request.unpaid_amount,
            balance_request.paid_amount,
            balance_request.total_earned,
        ]
    ):
        raise HTTPException(
            status_code=400, detail="At least one balance field must be provided"
        )

    try:
        current_balance = await get_user_balance(db, user)
        if not current_balance:
            raise HTTPException(status_code=404, detail="User balance not found")

        updated_balance = await update_user_balance_manually(
            db, user, balance_request, str(token)
        )

        return BalanceUpdateResponse(
            success=True,
            user=user,
            old_balance=UserBalance(**current_balance),
            new_balance=UserBalance(**updated_balance),
            message=f"Updated balance for {user}",
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating balance for {user}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
