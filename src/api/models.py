from typing import Any, Dict, List, Optional
from datetime import datetime
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
    shares_value_5m: float = Field(description="Total share difficulty value (5m)")
    shares_value_60m: float = Field(description="Total share difficulty value (60m)")
    shares_value_24h: float = Field(description="Total share difficulty value (24h)")


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


class WorkerDailyStats(BaseModel):
    """Worker statistics for a specific day"""

    shares: int = Field(description="Total shares submitted")
    share_value: float = Field(description="Total share difficulty value")
    hashrate: float = Field(description="Average hashrate in Gh/s")
    hash_rate_unit: str = Field(default="Gh/s", description="Hash rate unit")


class BtcWorkersShareValue(BaseModel):
    """Container for worker share value statistics with reward amount"""

    workers: Dict[str, WorkerDailyStats] = Field(
        description="Worker statistics for the date, keyed by worker name"
    )
    btc_amount: Optional[float] = Field(
        description="BTC reward amount for the date", default=None
    )
    paid: Optional[bool] = Field(
        description="Whether the reward has been paid out", default=None
    )
    payment_proof_url: Optional[str] = Field(
        description="URL to payment proof documentation", default=None
    )


class WorkersShareValueResponse(BaseModel):
    """Workers share value API response"""

    btc: BtcWorkersShareValue


class RewardRequest(BaseModel):
    """Request model for updating daily rewards"""

    amount: Optional[float] = Field(None, description="Reward amount in BTC", ge=0)
    paid: Optional[bool] = Field(
        None, description="Whether the reward has been paid out"
    )
    payment_proof_url: Optional[str] = Field(
        None, description="URL to payment proof documentation"
    )


class TidesConfig(BaseModel):
    """
    Model for updating the TIDES share window configuration.
    Supports partial updates - at least one field must be provided.
    """

    network_difficulty: Optional[float] = Field(
        None, gt=0, description="The target network difficulty for the share window."
    )
    multiplier: Optional[float] = Field(
        None,
        gt=0,
        description="The multiplier used to calculate the target share value.",
    )


class TidesRewardSummary(BaseModel):
    """TIDES reward summary information"""

    tx_hash: str = Field(description="Bitcoin transaction hash")
    btc_amount: float = Field(description="BTC reward amount")
    confirmed_at: datetime = Field(description="When the transaction was confirmed")
    processed: bool = Field(description="Whether this reward has been processed")


class TidesRewardsResponse(BaseModel):
    """TIDES rewards summary API response"""

    rewards: List[TidesRewardSummary] = Field(description="List of TIDES rewards")


class TidesRewardDetails(BaseModel):
    """Full TIDES reward details"""

    tx_hash: str = Field(description="Bitcoin transaction hash")
    block_height: int = Field(description="Bitcoin block height")
    btc_amount: float = Field(description="BTC reward amount")
    confirmed_at: datetime = Field(description="When the transaction was confirmed")
    discovered_at: datetime = Field(description="When this reward was discovered")
    tides_window: Dict[str, Any] = Field(
        description="TIDES window data at time of discovery"
    )
    processed: bool = Field(description="Whether this reward has been processed")
    updated_at: datetime = Field(description="Last update timestamp")


class TidesRewardUpdateRequest(BaseModel):
    """Request model for updating TIDES reward fields"""

    btc_amount: Optional[float] = Field(None, description="BTC reward amount", gt=0)
    processed: Optional[bool] = Field(
        None, description="Whether this reward has been processed"
    )


class TidesWindowCalculateRequest(BaseModel):
    """Request model for custom TIDES window calculation"""

    end_datetime: datetime = Field(
        description="Calculate TIDES window backwards from this datetime"
    )
