"""
FastAPI application for TaoHash mining statistics API.

Provides RESTful endpoints for querying mining pool and worker statistics.
"""

import os
import time
import asyncio
from typing import Any, Optional
from datetime import datetime, timezone

import aiohttp
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Security, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from ..storage.db import StatsDB
from ..utils.logger import get_logger
from .models import (
    HealthResponse,
    PoolStatsResponse,
    WorkersStatsResponse,
    WorkersTimerangeResponse,
    WorkersShareValueResponse,
    RewardRequest,
)

logger = get_logger(__name__)

# Rate limiting
limiter = Limiter(key_func=get_remote_address)

security = HTTPBearer()

API_TOKENS = set(
    token.strip()
    for token in os.environ.get("API_TOKENS", "").split(",")
    if token.strip()
)

REWARDS_POST_TOKEN = os.environ.get("REWARDS_POST_TOKEN", "")
ENABLE_REWARD_POLLING = os.environ.get("ENABLE_REWARD_POLLING", "")
REWARD_CHECK_INTERVAL = int(os.environ.get("REWARD_CHECK_INTERVAL", ""))
BRAIINS_API_TOKEN = os.environ.get("BRAIINS_API_TOKEN", "")
BRAIINS_API_URL = os.environ.get("BRAIINS_API_URL", "")

db: Optional[StatsDB] = None


async def process_rewards_task():
    """Bg task to automatically fetch and set daily rewards from Braiins Pool API."""
    logger.info("Starting daily rewards loop")

    if not BRAIINS_API_TOKEN:
        logger.error("BRAIINS_API_TOKEN not configured, disabling daily rewards loop")
        return

    while True:
        try:
            if not db or not db.client:
                logger.warning("Database not available for reward processing")
                await asyncio.sleep(300)
                continue

            async with aiohttp.ClientSession() as session:
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-SlushPool-Auth-Token": BRAIINS_API_TOKEN,
                }

                async with session.get(BRAIINS_API_URL, headers=headers) as response:
                    if response.status != 200:
                        logger.error(
                            f"Failed to fetch rewards from Braiins API: {response.status}"
                        )
                        await asyncio.sleep(300)
                        continue

                    data = await response.json()
                    daily_rewards = data.get("btc", {}).get("daily_rewards", [])

                    # Process last 10 days of rewards
                    for reward_data in daily_rewards[:10]:
                        unix_timestamp = reward_data.get("date")
                        total_reward = reward_data.get("total_reward")

                        if not unix_timestamp or not total_reward:
                            continue

                        # Convert Unix timestamp to date
                        reward_date = datetime.fromtimestamp(
                            unix_timestamp, tz=timezone.utc
                        ).date()
                        reward_amount = float(total_reward)
                        if reward_amount > 0:
                            adjusted_reward_amount = reward_amount / 0.975
                            reward_amount_after_fee = (
                                adjusted_reward_amount * 0.98
                            )  # 2% fee

                        # Check if reward already exists
                        check_query = """
                        SELECT amount, paid
                        FROM daily_rewards
                        WHERE date = %(date)s
                        LIMIT 1
                        """

                        result = await db.client.query(
                            check_query, parameters={"date": reward_date}
                        )

                        if not result.result_rows:
                            # Check if there's worker activity before inserting
                            activity_query = """
                            SELECT COUNT(DISTINCT worker) as worker_count
                            FROM worker_daily_share_value
                            WHERE date = %(date)s
                            """

                            activity_result = await db.client.query(
                                activity_query, parameters={"date": reward_date}
                            )

                            if (
                                activity_result.result_rows
                                and activity_result.result_rows[0][0] > 0
                            ):
                                # Insert new reward only if workers were active
                                insert_query = """
                                INSERT INTO daily_rewards (date, amount)
                                VALUES (%(date)s, %(amount)s)
                                """

                                await db.client.command(
                                    insert_query,
                                    parameters={
                                        "date": reward_date,
                                        "amount": reward_amount_after_fee,
                                    },
                                )

                                logger.info(
                                    f"Set reward for {reward_date}: {reward_amount_after_fee} BTC"
                                )
                            else:
                                logger.warning(
                                    f"No worker activity for {reward_date}, skipping reward"
                                )
                        else:
                            logger.debug(
                                f"Reward already exists for {reward_date}, keeping existing value"
                            )

        except Exception as e:
            logger.error(f"Error in reward processing task: {e}")

        await asyncio.sleep(REWARD_CHECK_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle."""
    global db
    db = StatsDB()
    if await db.init():
        logger.info("API connected to ClickHouse successfully")
    else:
        logger.warning("API running without database connection")

    task = None
    if ENABLE_REWARD_POLLING:
        logger.info("Daily rewards loop is enabled")
        task = asyncio.create_task(process_rewards_task())
    else:
        logger.info("Daily rewards loop is disabled")

    yield

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


async def verify_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    """Verify API token."""
    token = credentials.credentials
    if not API_TOKENS or token not in API_TOKENS:
        raise HTTPException(status_code=403, detail="Invalid API token")
    return token


async def verify_rewards_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    """Verify rewards POST token."""
    token = credentials.credentials
    if not REWARDS_POST_TOKEN or token != REWARDS_POST_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid rewards token")
    return token


@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check():
    """Health check endpoint (no auth required)."""
    return {
        "status": "ok",
        "timestamp": int(time.time()),
        "database": "connected" if db and db.client else "disconnected",
    }


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
        stats_5m = await _get_pool_stats_for_window("5m")
        stats_60m = await _get_pool_stats_for_window("60m")
        stats_24h = await _get_pool_stats_for_window("24h")

        worker_counts = await _get_worker_counts()

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
        workers = await _get_worker_stats(worker)

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

    if time_diff == 0:
        time_diff = 1

    try:
        start_dt = datetime.fromtimestamp(start_time)
        end_dt = datetime.fromtimestamp(end_time)

        query = """
        SELECT
            worker,
            CASE
                WHEN max(ts) > fromUnixTimestamp(%(end_time_int)s) - INTERVAL 10 MINUTE THEN 'ok'
                ELSE 'offline'
            END as state,
            toUnixTimestamp(max(ts)) as last_share,
            count() as shares,
            sum(actual_difficulty) as share_value,
            sum(pool_difficulty) * 4294967296 / %(duration)s as hashrate
        FROM shares
        WHERE ts >= %(start_time_dt)s AND ts < %(end_time_dt)s
        GROUP BY worker
        ORDER BY worker
        """

        params = {
            "start_time_dt": start_dt,
            "end_time_dt": end_dt,
            "end_time_int": end_time,
            "duration": time_diff,
        }

        result = await db.client.query(query, parameters=params)

        workers_dict = {}
        for row in result.result_rows:
            worker_name = row[0]
            workers_dict[worker_name] = {
                "state": row[1],
                "last_share": int(row[2]) if row[2] else None,
                "shares": int(row[3]),
                "share_value": float(row[4]),
                "hashrate": float(row[5]) / 1e9,
                "hash_rate_unit": "Gh/s",
            }

        return {"btc": {"workers": workers_dict}}

    except Exception as e:
        logger.error(f"Error fetching workers timerange data: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def _get_pool_stats_for_window(window: str) -> dict[str, Any]:
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
        logger.error(f"Error in _get_pool_stats_for_window: {e}")

    return {
        "active_workers": 0,
        "total_shares": 0,
        "accepted": 0,
        "rejected": 0,
        "total_difficulty": 0,
        "hashrate": 0,
        "share_value": 0,
    }


async def _get_worker_counts() -> dict[str, int]:
    """Get counts of workers in different states."""
    try:
        query = """
        SELECT 
            COUNT(DISTINCT CASE WHEN last_share_ts > now() - INTERVAL 120 MINUTE THEN worker END) as ok_workers,
            COUNT(DISTINCT CASE WHEN last_share_ts <= now() - INTERVAL 120 MINUTE THEN worker END) as off_workers
        FROM worker_pool_latest_share_mv
        """

        result = await db.client.query(query)

        if result.result_rows and result.result_rows[0]:
            row = result.result_rows[0]
            return {"ok_workers": int(row[0] or 0), "off_workers": int(row[1] or 0)}
    except Exception as e:
        logger.error(f"Error in _get_worker_counts: {e}")

    return {"ok_workers": 0, "off_workers": 0}


async def _get_worker_stats(
    worker: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Fetches all worker statistics which were active in the last 24 hours.
    """
    params = {}
    where_clause = ""

    if worker:
        where_clause = "WHERE worker = %(worker)s"
        params["worker"] = worker

    query = f"""
    WITH
        all_active_workers AS (
            SELECT DISTINCT worker
            FROM worker_stats_24h
            {where_clause}
        ),
        
        stats_5m AS (
            SELECT
                worker,
                argMax(miner, ts) as latest_miner,
                count() as shares,
                sum(actual_difficulty) as share_value,
                sum(pool_difficulty) * 4294967296 / 300 as hashrate
            FROM shares
            WHERE ts > now() - INTERVAL 5 MINUTE
            {"AND " + where_clause.replace("WHERE ", "") if where_clause else ""}
            GROUP BY worker
        )

    SELECT
        w.worker,
        -- Get miner from the most recent source available
        COALESCE(s5.latest_miner, s60.latest_miner, s24.latest_miner) as latest_miner,
        
        -- Get the live state and last share from the fast MV
        toUnixTimestamp(latest_share_data.last_share_ts) as last_share_ts,
        CASE
            WHEN latest_share_data.last_share_ts > now() - INTERVAL 10 MINUTE THEN 'ok'
            ELSE 'offline'
        END as state,

        s5.shares as shares_5m,
        s5.hashrate as hashrate_5m,
        s5.share_value as share_value_5m,

        s60.shares as shares_60m,
        s60.hashrate as hashrate_60m,
        s60.actual_difficulty_sum as share_value_60m,

        s24.shares as shares_24h,
        s24.hashrate as hashrate_24h,
        s24.actual_difficulty_sum as share_value_24h
        
    FROM all_active_workers AS w
    LEFT JOIN worker_stats_24h AS s24 ON w.worker = s24.worker
    LEFT JOIN worker_stats_60m AS s60 ON w.worker = s60.worker
    LEFT JOIN stats_5m AS s5 ON w.worker = s5.worker
    LEFT JOIN worker_pool_latest_share_mv AS latest_share_data ON w.worker = latest_share_data.worker
    ORDER BY w.worker
    """

    result = await db.client.query(query, parameters=params)

    workers = []
    for row in result.result_rows:
        workers.append(
            {
                "worker": row[0],
                "miner": row[1],
                "last_share_ts": row[2],
                "state": row[3],
                "shares_5m": row[4] or 0,
                "hashrate_5m": row[5] or 0,
                "share_value_5m": row[6] or 0,
                "shares_60m": row[7] or 0,
                "hashrate_60m": row[8] or 0,
                "share_value_60m": row[9] or 0,
                "shares_24h": row[10] or 0,
                "hashrate_24h": row[11] or 0,
                "share_value_24h": row[12] or 0,
            }
        )
    return workers


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

        query = """
        SELECT
            worker,
            countMerge(shares) as shares,
            sumMerge(share_value) as share_value,
            sumMerge(pool_difficulty_sum) * 4294967296 / 86400 as hashrate
        FROM worker_daily_share_value
        WHERE date = %(date)s
        GROUP BY worker
        ORDER BY worker
        """

        params = {"date": requested_date}
        result = await db.client.query(query, parameters=params)

        workers_dict = {}
        for row in result.result_rows:
            worker_name = row[0]
            workers_dict[worker_name] = {
                "shares": int(row[1]),
                "share_value": float(row[2]),
                "hashrate": float(row[3]) / 1e9,  # Convert to GH/s
                "hash_rate_unit": "Gh/s",
            }

        reward_query = """
        SELECT amount, paid, payment_proof_url
        FROM daily_rewards
        WHERE date = %(date)s
        LIMIT 1
        """

        reward_result = await db.client.query(reward_query, parameters=params)
        btc_amount = None
        paid = None
        payment_proof_url = None
        if reward_result.result_rows and reward_result.result_rows[0]:
            btc_amount = float(reward_result.result_rows[0][0])
            paid = (
                bool(reward_result.result_rows[0][1])
                if len(reward_result.result_rows[0]) > 1
                else False
            )
            payment_proof_url = (
                reward_result.result_rows[0][2]
                if len(reward_result.result_rows[0]) > 2
                else None
            )

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

        rewards = {}
        for row in result.result_rows:
            date_str = row[0].strftime("%Y-%m-%d")
            rewards[date_str] = {
                "amount": float(row[1]),
                "updated_at": row[2].isoformat() if row[2] else None,
                "paid": bool(row[3]) if len(row) > 3 else False,
                "payment_proof_url": row[4] if len(row) > 4 else "",
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
        total_unpaid = 0.0

        for row in result.result_rows:
            date_str = row[0].strftime("%Y-%m-%d")
            amount = float(row[1])
            unpaid_rewards.append(
                {
                    "date": date_str,
                    "amount": amount,
                    "updated_at": row[2].isoformat() if row[2] else None,
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
