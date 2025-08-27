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

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from src.rewards_extraction.monitor import rewards_monitor_task
from src.difficulty_monitoring.monitor import difficulty_monitor_task
from src.tides_monitoring.monitor import tides_monitor_task
from src.tides_monitoring.rewards_monitor import tides_rewards_monitor_task
from src.storage.db import StatsDB
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
    TidesRewardDetails,
    TidesRewardsResponse,
    TidesRewardUpdateRequest,
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
from src.api.services.tides_queries import (
    get_tides_window,
)
from src.api.services.tides_rewards_queries import (
    get_all_tides_rewards,
    get_tides_reward_by_tx_hash,
    update_tides_reward,
)

logger = get_logger(__name__)

# Rate limiting
limiter = Limiter(key_func=get_remote_address)

# Controllers configuration
ENABLE_REWARD_POLLING = os.environ.get("ENABLE_REWARD_POLLING", "")
ENABLE_DIFFICULTY_MONITORING = os.environ.get("ENABLE_DIFFICULTY_MONITORING", "")
ENABLE_TIDES_MONITORING = os.environ.get("ENABLE_TIDES_MONITORING", "")
ENABLE_TIDES_REWARDS_MONITORING = os.environ.get("ENABLE_TIDES_REWARDS_MONITORING", "")
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

    reward_task = None
    difficulty_task = None
    tides_task = None
    tides_rewards_task = None

    if ENABLE_REWARD_POLLING:
        logger.info("Daily rewards loop is enabled")
        reward_task = asyncio.create_task(rewards_monitor_task(db))
    else:
        logger.info("Daily rewards loop is disabled")

    if ENABLE_DIFFICULTY_MONITORING:
        logger.info("Difficulty monitoring is enabled")
        difficulty_task = asyncio.create_task(difficulty_monitor_task(db))
    else:
        logger.info("Difficulty monitoring is disabled")

    if ENABLE_TIDES_MONITORING:
        logger.info("TIDES monitoring is enabled")
        tides_task = asyncio.create_task(tides_monitor_task(db))
    else:
        logger.info("TIDES monitoring is disabled")

    if ENABLE_TIDES_REWARDS_MONITORING:
        logger.info("TIDES rewards monitoring is enabled")
        tides_rewards_task = asyncio.create_task(tides_rewards_monitor_task(db))
    else:
        logger.info("TIDES rewards monitoring is disabled")

    yield

    for task in [reward_task, difficulty_task, tides_task, tides_rewards_task]:
        if task:
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
            "shares_value_5m": stats_5m.get("share_value", 0),
            "shares_value_60m": stats_60m.get("share_value", 0),
            "shares_value_24h": stats_24h.get("share_value", 0),
            "pool_fee": POOL_FEE,
            "minimum_payout_threshold": MINIMUM_PAYOUT_THRESHOLD,
            "minimum_payout_threshold_unit": MINIMUM_PAYOUT_THRESHOLD_UNIT,
        }

    except Exception as e:
        logger.error(f"Error fetching stats summary: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/pool/stats", response_model=PoolStatsResponse, tags=["Historical Data"])
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
            "btc": {
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
                "shares_value_5m": stats_5m.get("share_value", 0),
                "shares_value_60m": stats_60m.get("share_value", 0),
                "shares_value_24h": stats_24h.get("share_value", 0),
            },
        }

        return response

    except Exception as e:
        logger.error(f"Error fetching pool stats: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get(
    "/api/workers/stats", response_model=WorkersStatsResponse, tags=["Historical Data"]
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

        return {"btc": {"workers": workers_dict}}

    except Exception as e:
        logger.error(f"Error fetching worker stats: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get(
    "/api/workers/timerange",
    response_model=WorkersTimerangeResponse,
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
            "btc": {
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

        if config_data["updated_at"]:
            config_data["updated_at"] = config_data["updated_at"].isoformat()

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
    "/api/tides/window",
    tags=["TIDES"],
    summary="Get TIDES Window",
    response_description="Current TIDES sliding window data",
)
async def get_tides_window_endpoint(
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
        tides_data = await get_tides_window(db)

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
