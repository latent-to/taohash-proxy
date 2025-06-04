"""
FastAPI application for TaoHash mining statistics API.

Provides RESTful endpoints for querying mining pool and worker statistics.
"""

import os
import time
from typing import Any, Optional
from datetime import datetime, timedelta

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

    yield

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
    request: Request, token: str = Depends(verify_token), pool: Optional[str] = None
) -> dict[str, Any]:
    """
    Get aggregated pool statistics.

    Args:
        pool: Optional pool filter - "all", "normal", "high_diff" (defaults to "all")

    Returns statistics for 5-minute, 60-minute, and 24-hour windows.

    **Requires ClickHouse database to be running.**
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        now = datetime.now()
        yesterday = now - timedelta(days=1)

        # Default to "all"
        pool_filter = pool if pool in ["normal", "high_diff"] else None

        stats_5m = await _get_pool_stats_for_window("5m", pool_filter)
        stats_60m = await _get_pool_stats_for_window("60m", pool_filter)
        stats_24h = await _get_pool_stats_for_window("24h", pool_filter)
        stats_yesterday = await _get_pool_stats_yesterday(yesterday, pool_filter)

        worker_counts = await _get_worker_counts(pool_filter)

        response = {
            "pool": pool or "all",
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
                "hash_rate_yesterday": stats_yesterday.get("hashrate", 0) / 1e9
                if stats_yesterday.get("hashrate", 0)
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
                "shares_yesterday": stats_yesterday.get("total_shares", 0),
                "shares_value_5m": stats_5m.get("share_value", 0),
                "shares_value_60m": stats_60m.get("share_value", 0),
                "shares_value_24h": stats_24h.get("share_value", 0),
                "shares_value_yesterday": stats_yesterday.get("share_value", 0),
            },
        }

        if not pool_filter:
            pools_included = set()
            for stat_window in [stats_5m, stats_60m, stats_24h, stats_yesterday]:
                if "pools_included" in stat_window:
                    pools_included.update(stat_window["pools_included"])

            response["pools_included"] = sorted(list(pools_included))

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
    pool: Optional[str] = None,
) -> dict[str, Any]:
    """
    Get per-worker statistics.

    Can filter by specific worker and pool if provided.

    Args:
        worker: Optional worker filter
        pool: Optional pool filter - "all", "normal", "high_diff" (defaults to "all")

    **Requires ClickHouse database to be running.**
    """
    if not db or not db.client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        # Default to "all"
        pool_filter = pool if pool in ["normal", "high_diff"] else None
        workers = await _get_worker_stats(worker, pool_filter)

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

    # Validate time range
    if start_time >= end_time:
        raise HTTPException(
            status_code=400, detail="start_time must be before end_time"
        )

    time_diff = end_time - start_time
    if time_diff > 30 * 24 * 3600:  # 30 days max
        raise HTTPException(status_code=400, detail="Time range cannot exceed 30 days")

    try:
        # Convert timestamps to datetime
        start_dt = datetime.fromtimestamp(start_time)
        end_dt = datetime.fromtimestamp(end_time)

        # Query for shares within time range and calculate metrics
        query = """
        WITH 
        -- Get current state and last share time for all workers
        current_state AS (
            SELECT 
                worker,
                max(ts) as last_share_ts,
                CASE 
                    WHEN max(ts) > now() - INTERVAL 10 MINUTE THEN 'ok'
                    ELSE 'offline'
                END as state
            FROM shares
            GROUP BY worker
        ),
        -- Get metrics for the time range
        timerange_stats AS (
            SELECT 
                worker,
                count() as shares,
                sum(actual_difficulty) as share_value,
                sum(pool_difficulty) * 4294967296 / %(duration)s as hashrate
            FROM shares
            WHERE ts >= %(start_time)s AND ts < %(end_time)s
            GROUP BY worker
        )
        SELECT 
            COALESCE(t.worker, c.worker) as worker,
            c.state,
            toUnixTimestamp(c.last_share_ts) as last_share,
            COALESCE(t.shares, 0) as shares,
            COALESCE(t.share_value, 0) as share_value,
            COALESCE(t.hashrate, 0) as hashrate
        FROM current_state c
        FULL OUTER JOIN timerange_stats t ON c.worker = t.worker
        WHERE t.worker IS NOT NULL  -- Only include workers with shares in range
        ORDER BY worker
        """

        params = {"start_time": start_dt, "end_time": end_dt, "duration": time_diff}

        result = await db.client.query(query, parameters=params)

        workers_dict = {}
        for row in result.result_rows:
            worker_name = row[0]
            workers_dict[worker_name] = {
                "state": row[1],
                "last_share": int(row[2]) if row[2] else None,
                "shares": int(row[3]),
                "share_value": float(row[4]),
                "hashrate": float(row[5]) / 1e9,  # Convert to GH/s
                "hash_rate_unit": "Gh/s",
            }

        return {"btc": {"workers": workers_dict}}

    except Exception as e:
        logger.error(f"Error fetching workers timerange data: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def _get_pool_stats_for_window(
    window: str, pool_filter: Optional[str] = None
) -> dict[str, Any]:
    """Get pool statistics for a specific time window."""
    try:
        if window == "5m":
            pool_condition = "AND pool_label = %(pool_filter)s" if pool_filter else ""
            params = {"pool_filter": pool_filter} if pool_filter else {}

            if pool_filter:
                query = f"""
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
                {pool_condition}
                """
            else:
                query = """
                SELECT 
                    count(DISTINCT worker) as active_workers,
                    count() as shares,
                    count() as accepted,
                    0 as rejected,
                    sum(pool_difficulty) as total_difficulty,
                    sum(actual_difficulty) as share_value,
                    sum(pool_difficulty) * 4294967296 / 300 as hashrate,
                    groupArray(DISTINCT pool_label) as pools_included
                FROM shares
                WHERE ts > now() - INTERVAL 5 MINUTE
                """
        else:
            # Using materialized views for longer windows
            view_name = f"pool_stats_{window}"
            where_clause = "WHERE pool_label = %(pool_filter)s" if pool_filter else ""
            params = {"pool_filter": pool_filter} if pool_filter else {}

            if pool_filter:
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
                {where_clause}
                """
            else:
                if window == "60m":
                    interval = "60 MINUTE"
                    time_seconds = 3600
                else:  # 24h
                    interval = "24 HOUR"
                    time_seconds = 86400

                query = f"""
                SELECT 
                    uniqMerge(unique_workers) as active_workers,
                    countMerge(total_shares) as shares,
                    countMerge(total_shares) as accepted,
                    0 as rejected,
                    sumMerge(sum_pool_difficulty) as total_difficulty,
                    sumMerge(sum_actual_difficulty) as share_value,
                    sumMerge(sum_pool_difficulty) * 4294967296 / {time_seconds} as hashrate,
                    groupArray(DISTINCT pool_label) as pools_included
                FROM pool_stats_mv
                WHERE ts > now() - INTERVAL {interval}
                """

        result = await db.client.query(query, parameters=params)

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

            if not pool_filter and len(row) > 7:
                response["pools_included"] = row[7]

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


async def _get_pool_stats_yesterday(
    yesterday: datetime, pool_filter: Optional[str] = None
) -> dict[str, Any]:
    """Get pool statistics for yesterday."""
    try:
        pool_condition = "AND pool_label = %(pool_filter)s" if pool_filter else ""

        if pool_filter:
            query = f"""
            SELECT 
                count(DISTINCT worker) as active_workers,
                count() as total_shares,
                count() as accepted,  -- All shares are accepted
                0 as rejected,        -- No rejected shares
                sum(pool_difficulty) as total_difficulty,
                sum(actual_difficulty) as share_value,
                sum(pool_difficulty) * 4294967296 / 86400 as hashrate
            FROM shares
            WHERE ts >= %(start)s AND ts < %(end)s
            {pool_condition}
            """
        else:
            query = """
            SELECT 
                count(DISTINCT worker) as active_workers,
                count() as total_shares,
                count() as accepted,  -- All shares stored are accepted
                0 as rejected,        -- No rejected shares
                sum(pool_difficulty) as total_difficulty,
                sum(actual_difficulty) as share_value,
                sum(pool_difficulty) * 4294967296 / 86400 as hashrate,
                groupArray(DISTINCT pool_label) as pools_included
            FROM shares
            WHERE ts >= %(start)s AND ts < %(end)s
            """

        params = {
            "start": yesterday.replace(hour=0, minute=0, second=0, microsecond=0),
            "end": yesterday.replace(hour=23, minute=59, second=59, microsecond=999999),
        }
        if pool_filter:
            params["pool_filter"] = pool_filter

        result = await db.client.query(query, parameters=params)

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

            if not pool_filter and len(row) > 7:
                response["pools_included"] = row[7]

            return response
    except Exception as e:
        logger.error(f"Error in _get_pool_stats_yesterday: {e}")

    return {
        "active_workers": 0,
        "total_shares": 0,
        "accepted": 0,
        "rejected": 0,
        "total_difficulty": 0,
        "hashrate": 0,
        "share_value": 0,
    }


async def _get_worker_counts(pool_filter: Optional[str] = None) -> dict[str, int]:
    """Get counts of workers in different states."""
    try:
        pool_condition = "WHERE pool_label = %(pool_filter)s" if pool_filter else ""
        params = {"pool_filter": pool_filter} if pool_filter else {}

        query = f"""
        SELECT 
            sum(CASE WHEN state = 'ok' THEN 1 ELSE 0 END) as ok_workers,
            sum(CASE WHEN state = 'offline' THEN 1 ELSE 0 END) as off_workers
        FROM worker_state
        {pool_condition}
        """

        result = await db.client.query(query, parameters=params)

        if result.result_rows and result.result_rows[0]:
            row = result.result_rows[0]
            return {"ok_workers": int(row[0] or 0), "off_workers": int(row[1] or 0)}
    except Exception as e:
        logger.error(f"Error in _get_worker_counts: {e}")

    return {"ok_workers": 0, "off_workers": 0}


async def _get_worker_stats(
    worker: Optional[str] = None,
    pool_filter: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Get worker statistics using optimized materialized views."""
    where_conditions = []
    params = {}

    if worker:
        where_conditions.append("worker = %(worker)s")
        params["worker"] = worker

    if pool_filter:
        where_conditions.append("pool_label = %(pool_filter)s")
        params["pool_filter"] = pool_filter

    where_clause = f"WHERE {' AND '.join(where_conditions)}" if where_conditions else ""

    query = f"""
    WITH 
    -- Get worker state first
    worker_status AS (
        SELECT 
            worker,
            latest_miner,
            pool_label,
            last_share_ts,
            state
        FROM worker_state
        {where_clause}
    ),
    -- Query shares directly for 5-minute precision
    stats_5m AS (
        SELECT 
            worker,
            count() as shares,
            sum(actual_difficulty) as share_value,
            sum(pool_difficulty) * 4294967296 / 300 as hashrate
        FROM shares
        WHERE ts > now() - INTERVAL 5 MINUTE
        {f"AND {' AND '.join(where_conditions)}" if where_conditions else ""}
        GROUP BY worker
    ),
    -- Use materialized views for 60m and 24h
    stats_60m AS (
        SELECT 
            worker,
            sum(shares) as shares,
            sum(actual_difficulty_sum) as share_value,
            sum(hashrate) as hashrate
        FROM worker_stats_60m
        {where_clause}
        GROUP BY worker
    ),
    stats_24h AS (
        SELECT 
            worker,
            sum(shares) as shares,
            sum(actual_difficulty_sum) as share_value,
            sum(hashrate) as hashrate
        FROM worker_stats_24h
        {where_clause}
        GROUP BY worker
    )
    SELECT 
        w.worker,
        w.latest_miner,
        w.pool_label,
        toUnixTimestamp(w.last_share_ts) as last_share_ts,
        w.state,
        ifNull(s5.shares, 0) as shares_5m,
        ifNull(s5.hashrate, 0) as hashrate_5m,
        ifNull(s5.share_value, 0) as share_value_5m,
        ifNull(s60.shares, 0) as shares_60m,
        ifNull(s60.hashrate, 0) as hashrate_60m,
        ifNull(s60.share_value, 0) as share_value_60m,
        ifNull(s24.shares, 0) as shares_24h,
        ifNull(s24.hashrate, 0) as hashrate_24h,
        ifNull(s24.share_value, 0) as share_value_24h
    FROM worker_status w
    LEFT JOIN stats_5m s5 ON w.worker = s5.worker
    LEFT JOIN stats_60m s60 ON w.worker = s60.worker
    LEFT JOIN stats_24h s24 ON w.worker = s24.worker
    ORDER BY w.worker
    """

    result = await db.client.query(query, parameters=params)

    workers = []
    for row in result.result_rows:
        workers.append(
            {
                "worker": row[0],
                "miner": row[1],
                "pool_label": row[2],
                "last_share_ts": row[3],
                "state": row[4],
                "shares_5m": row[5],
                "hashrate_5m": row[6],
                "share_value_5m": row[7],
                "shares_60m": row[8],
                "hashrate_60m": row[9],
                "share_value_60m": row[10],
                "shares_24h": row[11],
                "hashrate_24h": row[12],
                "share_value_24h": row[13],
            }
        )

    return workers
