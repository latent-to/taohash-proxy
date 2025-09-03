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


class CustomTidesRewardRequest(BaseModel):
    """Request model for manually creating TIDES rewards"""

    tx_hash: str = Field(description="Bitcoin transaction hash")
    block_height: int = Field(description="Bitcoin block height", gt=0)
    btc_amount: float = Field(description="BTC reward amount", gt=0)
    confirmed_at: datetime = Field(description="When the transaction was confirmed")


class CustomTidesRewardResponse(BaseModel):
    """Response model for created TIDES reward"""

    tx_hash: str = Field(description="Bitcoin transaction hash")
    block_height: int = Field(description="Bitcoin block height")
    btc_amount: float = Field(description="BTC reward amount")
    confirmed_at: datetime = Field(description="When the transaction was confirmed")
    discovered_at: datetime = Field(description="When this reward was discovered")
    tides_window: Dict[str, Any] = Field(
        description="TIDES window data at time of discovery"
    )
    processed: bool = Field(description="Whether this reward has been processed")


class EarningRecord(BaseModel):
    """Individual earning record"""

    earning_id: str = Field(description="Unique earning identifier")
    worker: str = Field(description="Worker who earned this")
    btc_amount: float = Field(description="BTC amount earned")
    earning_type: str = Field(description="Type of earning (tides, pplns, manual)")
    reference: Optional[str] = Field(description="Reference to source (tx_hash, batch_id, etc.)")
    tides_reward_id: Optional[str] = Field(description="Link to tides_rewards.tx_hash if applicable")
    metadata: Dict[str, Any] = Field(description="Additional context (percentages, window info, etc.)")
    earned_at: datetime = Field(description="When this was earned")
    created_at: datetime = Field(description="When record was created")


class EarningsResponse(BaseModel):
    """Response model for worker earnings"""

    earnings: List[EarningRecord] = Field(description="List of earning records")
    total_count: Optional[int] = Field(description="Total count without pagination")


class CreateEarningRequest(BaseModel):
    """Request model for creating manual earnings"""

    worker: str = Field(description="Worker name")
    btc_amount: float = Field(description="BTC amount to credit", gt=0)
    earning_type: str = Field(default="manual", description="Type of earning")
    reference: Optional[str] = Field(None, description="Optional reference")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Optional metadata")
    earned_at: Optional[datetime] = Field(None, description="When earned (defaults to now)")


class UpdateEarningRequest(BaseModel):
    """Request model for updating existing earnings"""

    btc_amount: Optional[float] = Field(None, description="New BTC amount", gt=0)
    metadata: Optional[Dict[str, Any]] = Field(None, description="New metadata")
    reference: Optional[str] = Field(None, description="New reference")


class EarningOperationResponse(BaseModel):
    """Response model for earning operations (create/update/delete)"""

    success: bool = Field(description="Whether operation succeeded")
    earning_id: Optional[str] = Field(description="Earning ID (for create/update)")
    message: str = Field(description="Operation result message")


class PayoutItem(BaseModel):
    """Individual worker payout in a batch"""

    worker: str = Field(description="Worker name")
    btc_amount: float = Field(description="BTC amount to pay out", gt=0)


class BatchPayoutRequest(BaseModel):
    """Request model for creating batch payouts"""

    payouts: List[PayoutItem] = Field(description="List of worker payouts")
    bitcoin_tx_hash: str = Field(description="Bitcoin transaction hash for the batch")
    payment_method: str = Field(default="bitcoin", description="Payment method")
    notes: str = Field(default="", description="Admin notes about this payout")
    admin_override: bool = Field(default=False, description="Allow negative balances if true")
    tides_tx_hash: Optional[str] = Field(None, description="TIDES reward to mark as processed")


class ValidationFailure(BaseModel):
    """Balance validation failure details"""

    worker: str = Field(description="Worker with insufficient balance")
    current_balance: float = Field(description="Current unpaid balance")
    payout_requested: float = Field(description="Requested payout amount")
    net_balance: float = Field(description="Resulting balance after payout")


class NegativeBalanceWarning(BaseModel):
    """Warning for negative balance after payout"""

    worker: str = Field(description="Worker who will have negative balance")
    current_balance: float = Field(description="Current unpaid balance")
    payout_requested: float = Field(description="Requested payout amount")
    net_balance: float = Field(description="Resulting negative balance")


class BatchPayoutResponse(BaseModel):
    """Response model for batch payout operations"""

    success: bool = Field(description="Whether batch payout succeeded")
    batch_id: Optional[str] = Field(description="Created batch ID")
    total_amount: Optional[float] = Field(description="Total BTC amount in batch")
    processed_workers: Optional[int] = Field(description="Number of workers processed")
    admin_override_used: Optional[bool] = Field(description="Whether admin override was used")
    error: Optional[str] = Field(description="Error message if failed")
    validation_failures: Optional[List[ValidationFailure]] = Field(description="Balance validation failures")
    negative_balance_warnings: Optional[List[NegativeBalanceWarning]] = Field(description="Negative balance warnings")
    suggestion: Optional[str] = Field(description="Suggestion for fixing validation failures")


class PayoutRecord(BaseModel):
    """Individual payout record"""

    payout_id: str = Field(description="Unique payout identifier")
    worker: str = Field(description="Worker who received payout")
    btc_amount: float = Field(description="BTC amount paid out")
    payout_batch_id: Optional[str] = Field(description="Link to batch if part of batch payout")
    bitcoin_tx_hash: str = Field(description="Bitcoin transaction hash")
    notes: str = Field(description="Payout notes")
    paid_at: datetime = Field(description="When payout was made")
    created_at: datetime = Field(description="When record was created")


class PayoutsResponse(BaseModel):
    """Response model for payout queries"""

    payouts: List[PayoutRecord] = Field(description="List of payout records")
    total_count: Optional[int] = Field(description="Total count without pagination")


class BatchPayoutDetails(BaseModel):
    """Detailed batch payout information"""

    batch_id: str = Field(description="Batch identifier")
    total_amount: float = Field(description="Total BTC amount in batch")
    user_count: int = Field(description="Number of users in batch")
    payment_method: str = Field(description="Payment method used")
    external_reference: str = Field(description="Bitcoin tx hash or bank reference")
    notes: str = Field(description="Admin notes")
    processed_at: datetime = Field(description="When batch was processed")
    processed_by: str = Field(description="Admin who processed")
    created_at: datetime = Field(description="When batch was created")
    individual_payouts: List[PayoutRecord] = Field(description="All payouts in this batch")


class UpdatePayoutRequest(BaseModel):
    """Request model for updating individual payouts"""

    btc_amount: Optional[float] = Field(None, description="New BTC amount", gt=0)
    bitcoin_tx_hash: Optional[str] = Field(None, description="New Bitcoin tx hash")
    notes: Optional[str] = Field(None, description="New notes")


class PayoutOperationResponse(BaseModel):
    """Response model for payout operations (update/delete)"""

    success: bool = Field(description="Whether operation succeeded")
    payout_id: Optional[str] = Field(description="Payout ID")
    message: str = Field(description="Operation result message")


class WorkerBalance(BaseModel):
    """Individual worker balance information"""

    worker: str = Field(description="Worker name")
    unpaid_amount: float = Field(description="Current unpaid balance")
    paid_amount: float = Field(description="Total amount paid out")
    total_earned: float = Field(description="Total amount earned")
    last_updated: datetime = Field(description="When balance was last updated")
    updated_by: str = Field(description="What system/process last updated this")


class BalanceResponse(BaseModel):
    """Response model for single worker balance"""

    balance: WorkerBalance = Field(description="Worker balance information")


class BalancesResponse(BaseModel):
    """Response model for all worker balances"""

    balances: List[WorkerBalance] = Field(description="List of all worker balances")
    total_count: int = Field(description="Total number of workers with balances")
