# TaoHash Mining API Documentation

REST API reference for TaoHash mining statistics endpoints. 

> **📚 Interactive API Documentation**: Visit `http://localhost:8888/docs` for Swagger UI with try-it-out functionality and `http://localhost:8888/redoc`.

## Quick Start Examples

```bash
# Pool stats
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8888/api/pool/stats | jq .

# Worker stats
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8888/api/workers/stats | jq .

# Worker stats for time range
curl -H "Authorization: Bearer YOUR_TOKEN" \
  "http://localhost:8888/api/workers/timerange?start_time=1749000000&end_time=1749003600" | jq .
```

## Authentication

All API endpoints require Bearer token authentication.

### Setting Up Authentication
1. Generate a token:
   ```bash
   python scripts/generate_token.py
   ```

2. Add to your `.env` file:
   ```bash
   API_TOKENS=your_generated_token,another_token_if_needed
   ```

3. Include in requests:
   ```bash
   curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8888/api/pool/stats
   ```

### Authentication Errors
- **401 Unauthorized**: Missing or invalid token
- **403 Forbidden**: Token lacks required permissions

## Base URL

- **API Server**: `http://your-server:8888`
- **Interactive Docs**: `http://your-server:8888/docs`
- **ReDoc**: `http://your-server:8888/redoc`

## Administrative Endpoints

### Configuration Hot Reload
Reload the proxy configuration without restarting the service. Updates pool settings from `config.toml` dynamically.

**Endpoint:** `POST /api/reload`  
**Authentication:** Not required  
**Port:** 5001 (separate from main API)

**Example:**
```bash
curl -X POST http://localhost:5001/api/reload
```

**Response:**
```text
Reload scheduled
```

**Error Response:**
```text
Failed to update config
```

**Notes:**
- Runs on separate port (5001) from main API endpoints
- Existing miner connections will be gracefully closed
- Miners will automatically reconnect with updated configuration

**Important:** This port is not exposed by default and should not be exposed by any validator. If exposed, this can be a security risk. 

## Core API Endpoints

### Health Check
Check if the API service is running and database connectivity status.

**Endpoint:** `GET /health`  
**Authentication:** Not required

**Example:**
```bash
curl http://localhost:8888/health
```

**Response:**
```json
{
  "status": "ok",
  "timestamp": 1749000000,
  "database": "connected"
}
```

### Pool Statistics
Get aggregated mining pool statistics with time-based metrics.

**Endpoint:** `GET /api/pool/stats`  
**Authentication:** Required  
**Query Parameters:**
- `pool` (optional): Filter by pool - "normal", "high_diff", or omit for "all"

**Example:**
```bash
# Get all pools statistics
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:8888/api/pool/stats

# Get specific pool statistics
curl -H "Authorization: Bearer YOUR_TOKEN" \
  "http://localhost:8888/api/pool/stats?pool=high_diff"
```

**Response:**
```json
{
  "pool": "all",
  "btc": {
    "all_time_reward": "0.00000000",
    "hash_rate_unit": "Gh/s",
    "hash_rate_5m": 1861.8396897006933,
    "hash_rate_60m": 0,
    "hash_rate_24h": 0,
    "hash_rate_yesterday": 0,
    "low_workers": 0,
    "off_workers": 0,
    "ok_workers": 2,
    "dis_workers": 0,
    "current_balance": "0.00000000",
    "today_reward": "0.00000000",
    "estimated_reward": "0.00000000",
    "shares_5m": 97,
    "shares_60m": 0,
    "shares_24h": 0,
    "shares_yesterday": 0,
    "shares_value_5m": 1388261.0,
    "shares_value_60m": 0,
    "shares_value_24h": 0,
    "shares_value_yesterday": 0.0
  },
  "pools_included": [
    "high_diff",
    "normal"
  ]
}
```

### Worker Statistics
Get detailed statistics for connected workers with time-based metrics.

**Endpoint:** `GET /api/workers/stats`  
**Authentication:** Required  
**Query Parameters:**
- `worker` (optional): Filter by worker name
- `pool` (optional): Filter by pool - "normal", "high_diff", or omit for all

**Example:**
```bash
# Get all workers statistics
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:8888/api/workers/stats

# Get workers for specific pool
curl -H "Authorization: Bearer YOUR_TOKEN" \
  "http://localhost:8888/api/workers/stats?pool=high_diff"

# Get specific worker
curl -H "Authorization: Bearer YOUR_TOKEN" \
  "http://localhost:8888/api/workers/stats?worker=abelatent.worker01"
```

**Response:**
```json
{
  "btc": {
    "workers": {
      "abelatent.worker01": {
        "state": "ok",
        "last_share": 1749000553,
        "hash_rate_unit": "Gh/s",
        "hash_rate_scoring": 0,
        "hash_rate_5m": 820.9686820727467,
        "hash_rate_60m": 707.3524805358933,
        "hash_rate_24h": 29.47302002232889,
        "shares_5m": 28,
        "shares_60m": 504,
        "shares_24h": 504,
        "share_value_5m": 854502.0,
        "share_value_60m": 4495805.0,
        "share_value_24h": 4495805.0
      },
      "abelatent.worker02": {
        "state": "ok",
        "last_share": 1749000546,
        "hash_rate_unit": "Gh/s",
        "hash_rate_scoring": 0,
        "hash_rate_5m": 967.57023244288,
        "hash_rate_60m": 746.4462273012622,
        "hash_rate_24h": 31.101926137552592,
        "shares_5m": 66,
        "shares_60m": 548,
        "shares_24h": 548,
        "share_value_5m": 464129.0,
        "share_value_60m": 4295355.0,
        "share_value_24h": 4295355.0
      }
    }
  }
}
```


### Workers Time Range Statistics
Get worker statistics for a custom time range.

**Endpoint:** `GET /api/workers/timerange`  
**Authentication:** Required  
**Query Parameters:**
- `start_time` (required): Start time as Unix timestamp
- `end_time` (required): End time as Unix timestamp

**Example:**
```bash
# Get worker stats for a 1-hour period
curl -H "Authorization: Bearer YOUR_TOKEN" \
  "http://localhost:8888/api/workers/timerange?start_time=1749000000&end_time=1749003600"
```

**Response:**
```json
{
  "btc": {
    "workers": {
      "abelatent.worker01": {
        "hashrate": 950.5,
        "shares": 45,
        "share_value": 250000.0,
        "hash_rate_unit": "Gh/s",
        "state": "ok",
        "last_share": 1749003590
      }
    }
  }
}
```

## Error Handling

### Standard Error Response
All errors return a consistent JSON format:

```json
{
  "error": {
    "code": "INVALID_TOKEN",
    "message": "The provided authentication token is invalid",
    "timestamp": "2024-01-20T10:30:00Z",
    "request_id": "abc-123-def"
  }
}
```

## Rate Limiting

All API endpoints are rate-limited to prevent abuse:

- **Default limit**: 60 requests per minute
- **Applied per**: IP address

Rate limit headers:
```
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 55
X-RateLimit-Reset: 1705750260
```


## SDK Examples

### Python
```python
import requests

class TaoHashAPI:
    def __init__(self, base_url, token):
        self.base_url = base_url
        self.headers = {'Authorization': f'Bearer {token}'}
    
    def get_pool_stats(self):
        response = requests.get(
            f'{self.base_url}/api/pool/stats',
            headers=self.headers
        )
        return response.json()

# Usage
api = TaoHashAPI('http://localhost:8888', 'YOUR_TOKEN')
stats = api.get_pool_stats()
```