from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Health check response"""

    status: str = Field(description="Service status")
    timestamp: int = Field(description="Unix timestamp")
    database: str = Field(
        description="Database connection status: 'connected' or 'disconnected'"
    )


class BtcPoolStats(BaseModel):
    """Bitcoin pool statistics"""

    all_time_reward: str = Field(description="Total rewards earned (placeholder)")
    hash_rate_unit: str = Field(description="Hash rate unit (always Gh/s)")
    hash_rate_5m: float = Field(description="5-minute average hashrate in Gh/s")
    hash_rate_60m: float = Field(description="60-minute average hashrate in Gh/s")
    hash_rate_24h: float = Field(description="24-hour average hashrate in Gh/s")
    hash_rate_yesterday: float = Field(
        description="Yesterday's average hashrate in Gh/s"
    )
    low_workers: int = Field(description="Number of low hashrate workers (always 0)")
    off_workers: int = Field(description="Number of offline workers")
    ok_workers: int = Field(description="Number of active workers")
    dis_workers: int = Field(description="Number of disabled workers (always 0)")
    current_balance: str = Field(description="Current balance (placeholder)")
    today_reward: str = Field(description="Today's rewards (placeholder)")
    estimated_reward: str = Field(description="Estimated rewards (placeholder)")
    shares_5m: int = Field(description="Shares submitted in last 5 minutes")
    shares_60m: int = Field(description="Shares submitted in last 60 minutes")
    shares_24h: int = Field(description="Shares submitted in last 24 hours")
    shares_yesterday: int = Field(description="Shares submitted yesterday")
    shares_value_5m: float = Field(description="Total share difficulty value (5m)")
    shares_value_60m: float = Field(description="Total share difficulty value (60m)")
    shares_value_24h: float = Field(description="Total share difficulty value (24h)")
    shares_value_yesterday: float = Field(
        description="Total share difficulty value (yesterday)"
    )


class PoolStatsResponse(BaseModel):
    """Pool statistics API response"""

    pool: str = Field(description="Pool filter: 'all', 'normal', or 'high_diff'")
    btc: BtcPoolStats
    pools_included: Optional[List[str]] = Field(
        default=None, description="List of pools included in 'all' stats"
    )


class WorkerStats(BaseModel):
    """Individual worker statistics"""

    state: str = Field(description="Worker state: 'ok' or 'offline'")
    last_share: int = Field(description="Unix timestamp of last share submission")
    hash_rate_unit: str = Field(description="Hash rate unit (always Gh/s)")
    hash_rate_scoring: float = Field(
        description="Scoring hashrate (placeholder, always 0)"
    )
    hash_rate_5m: float = Field(description="5-minute average hashrate in Gh/s")
    hash_rate_60m: float = Field(description="60-minute average hashrate in Gh/s")
    hash_rate_24h: float = Field(description="24-hour average hashrate in Gh/s")
    shares_5m: int = Field(description="Shares submitted in last 5 minutes")
    shares_60m: int = Field(description="Shares submitted in last 60 minutes")
    shares_24h: int = Field(description="Shares submitted in last 24 hours")
    share_value_5m: float = Field(description="Total share difficulty value (5m)")
    share_value_60m: float = Field(description="Total share difficulty value (60m)")
    share_value_24h: float = Field(description="Total share difficulty value (24h)")


class BtcWorkers(BaseModel):
    """Container for worker statistics"""

    workers: Dict[str, WorkerStats] = Field(
        description="Worker statistics keyed by worker name"
    )


class WorkersStatsResponse(BaseModel):
    """Workers statistics API response"""

    btc: BtcWorkers


class WorkerTimerangeStats(BaseModel):
    """Worker statistics for a custom time range"""

    hashrate: float = Field(description="Average hashrate over the time period in Gh/s")
    shares: int = Field(description="Total shares submitted in the time range")
    share_value: float = Field(description="Total share difficulty value")
    hash_rate_unit: str = Field(default="Gh/s", description="Hash rate unit")
    state: Optional[str] = Field(description="Current worker state: 'ok' or 'offline'")
    last_share: Optional[int] = Field(
        description="Unix timestamp of last share submission"
    )


class BtcWorkersTimerange(BaseModel):
    """Container for worker timerange statistics"""

    workers: Dict[str, WorkerTimerangeStats] = Field(
        description="Worker statistics for the time range, keyed by worker name"
    )


class WorkersTimerangeResponse(BaseModel):
    """Workers timerange API response"""

    btc: BtcWorkersTimerange
