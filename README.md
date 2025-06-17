<div align="center">

# **TAO Hash Mining Proxy** ![Subnet 14](https://img.shields.io/badge/Subnet-14_%CE%BE-blue)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/latent-to/taohash)

</div>

# Quick Start

```bash
# Clone the repository
git clone https://github.com/latent-to/taohash-proxy
cd taohash-proxy/docker

# Configure environment
cp .env.example .env
# Edit .env with your AWS credentials and settings

# Start all services
docker-compose up -d

# View logs
docker-compose logs -f

# Miners can connect to:
# stratum+tcp://your-server:3331 (normal difficulty)
# stratum+tcp://your-server:3332 (high difficulty)
```

# Features

- **Multi-Pool Support**: Route different miners to different pools based on port
- **S3 Tiered Storage**: Automatic data archival - shares older than 2 days are automatically transferred to S3 storage
- **Real-time Dashboard**: Get a glimpse of connected miners and their statistics in the dashboard page. 
- **Analytics Dashboard**: Supports querying and filtering of historical share data
- **REST API**: Statistics API on port 8888 with bearer token auth
- **ClickHouse Database**: Time-series storage with automatic data lifecycle
- **Worker Tracking**: Individual worker performance metrics and share validation
- **Hot Configuration Reload**: Update pool settings without restarting. This is useful when validators want to change pools without disrupting miner operations. Check api.md for more details. 
- **Share Difficulty**: Creates the share header and calculates actual difficulty of the submission. This is used in scoring. 

# Configuration

### 1. Environment Variables
   ```bash
   cd docker
   cp .env.example .env
   ```
   
   Mandatory variables to be updated `.env`:
   - AWS credentials for S3 storage (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_S3_BUCKET)
   - ClickHouse password (CLICKHOUSE_PASSWORD)
   - API tokens (API_TOKENS)

### 2. Pool Configuration
 - Support is now added for both, normal miners and high difficulty miners. 
 - If you are using Braiins, you can set both difficulties with the same address; just a new port to support high difficulty miner's logic at the miners side. 

   Edit `config/config.toml`:
   ```toml
   [pools.normal]
   host = "stratum.pool.com"
   port = 3333
   user = "your_wallet.taohash"
   pass = "x"

   [pools.high_diff]
   host = "stratum.pool.com"
   port = 3333
   user = "your_wallet.taohash"
   pass = "x"
   ```

### 3. Generate API Tokens
   ```bash
   python scripts/generate_token.py
   # Add the generated token to your .env file
   ```

# Ports

- **3331**: Mining proxy (normal difficulty)
- **3332**: Mining proxy (high difficulty)  
- **5000**: Web dashboard & analytics
- **8888**: REST API (requires Bearer token authentication)
- **8123**: ClickHouse HTTP interface (internal - can be used to execute SQL queries)

# API Usage

```bash
# Pool stats
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8888/api/pool/stats | jq .

# Worker stats
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8888/api/workers/stats | jq .
```
# TaoHash Validator Mechanics

The proxy acts as a bridge between miners and mining pools, while collecting data for validator scoring:

### Port Configuration
- **Two TCP ports**: 3331 (normal difficulty) and 3332 (high difficulty)
- Each port connects to its configured mining pool
- If the pool doesn't support high difficulty, both ports should use the same pool configuration

### Minimum Difficulty Enforcement
The proxy can enforce minimum difficulty for pools that don't support custom difficulty:

1. Miners request minimum difficulty via password: `x;md=10000;`
2. Proxy intercepts pool's difficulty adjustments
3. Sends the higher value between pool difficulty and minimum requested
4. Once pool difficulty exceeds minimum, proxy stops intervening

### Data Collection
- **API endpoints** provide share statistics for validator scoring
- **Real-time dashboard** monitors connected miners and hashrates
- **Historical data** stored in ClickHouse for performance analysis 

# Documentation

- [Setup Guide](docs/SETUP.md) - Detailed installation instructions
- [API Reference](docs/API.md) - REST API endpoints

# Requirements

- Docker and Docker Compose
- AWS Account with S3 access
- Mining pool credentials
- Python 3.11+

## Contributing

We welcome contributions to improve this Bitcoin mining proxy. While designed for TaoHash (Bittensor Subnet 14), it works for any mining operation.
While efforts have been made to ensure compatibility with all miner types (Including ASIC Boost etc,), we welcome feedback and contributions to handle other types or edge-cases. 

### How to Contribute
- **Report bugs**: Open an issue with steps to reproduce
- **Request features**: Describe your use case and proposed solution
- **Submit PRs**: Fork, make changes, and submit a pull request
