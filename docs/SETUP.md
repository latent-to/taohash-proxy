# TaoHash Proxy Setup Guide

This guide will walk you through setting up the mining/validator proxy  with all required components including AWS S3 storage for data archival.

## Table of Contents
1. [Prerequisites](#prerequisites)
2. [AWS S3 Setup](#aws-s3-setup)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Running the Proxy](#running-the-proxy)
6. [Monitoring](#monitoring)
7. [Troubleshooting](#troubleshooting)

## Prerequisites

### System Requirements
- **OS**: Linux or macOS
- **CPU**: 2+ cores recommended
- **RAM**: 4GB minimum, 8GB recommended
- **Storage**: 60GB for local data (2 days retention before S3 archival)
- **Network**: Stable internet connection with open ports

### Software Requirements
- Docker 20.10+
- Docker Compose 2.0+
- Git
- AWS Account with S3 access
- Python 3.10+

### Install Docker
```bash
# Ubuntu/Debian
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER
# Log out and back in for group changes to take effect

# macOS
# Install Docker Desktop from https://www.docker.com/products/docker-desktop
```

## AWS S3 Setup (For validators)

S3 storage is required for archiving historical data. Data older than 2 days is automatically moved from local disk to S3, keeping your local storage requirements minimal while preserving all historical data.

### 1. Create S3 Bucket

1. Navigate to S3 → Create bucket (We recommend using S3 Express One Zone - it will be useful when we introduce BTC disbursements in the next phase)
2. Bucket name: your-taohash-bucket (must be globally unique)
3. Region: Choose closest to your proxy location
4. Block all public access: ✓ (keep enabled for security)

### 2. Create IAM User and Policy
1. Go to AWS IAM Console
2. Users → Add User
3. User name: `taohash-proxy`
4. Access type: ✓ Programmatic access
5. Click "Next: Permissions"
6. Click "Attach policies directly" → "Create policy"
7. Choose JSON tab and paste:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "TaoHashS3Access",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": [
        "arn:aws:s3:::your-taohash-bucket",
        "arn:aws:s3:::your-taohash-bucket/*"
      ]
    }
  ]
}
```
Don't forget to rename the `your-taohash-bucket` to your created bucket name. 

8. Name the policy: `TaoHashS3Policy`
9. Complete user creation
10. **Important**: Save the Access Key ID and Secret Access Key

## Installation

### 1. Clone Repository
```bash
git clone https://github.com/yourusername/taohash-proxy.git
cd taohash-proxy
```

### 2. Configure Environment Variables
```bash
cd docker
cp .env.example .env
```

Edit `.env` file with your settings:
```bash
# Required: AWS S3 Configuration
AWS_ACCESS_KEY_ID=your_access_key_from_step_2
AWS_SECRET_ACCESS_KEY=your_secret_key_from_step_2  
AWS_S3_BUCKET=your-taohash-bucket

# Required: ClickHouse Configuration
CLICKHOUSE_PASSWORD=generate_secure_password_here

# Required: API Configuration  
API_TOKENS=generate_token_here,add_more_if_needed
```

### 3. Generate API Token
```bash
# From project root
python3 scripts/generate_token.py
# Copy the generated token to API_TOKENS in .env
```

### 4. Configure Mining Pools
Edit `config/config.toml`:
```toml
# Normal difficulty pool
[pools.normal]
host = "stratum.braiins.com" 
port = 3333                  
user = "your_wallet/pool_name.taohash"
pass = "x"                  
proxy_port = 3331           

# High difficulty pool
[pools.high_diff]
host = "stratum.braiins.com"
port = 3333
user = "your_wallet/pool_name.taohash"
pass = "x"
proxy_port = 3332
```

## Running the Proxy

### 1. Start All Services
```bash
cd docker
docker-compose up -d
```

This will start:
- TaoHash proxy (2 instances for different difficulties)
- ClickHouse database with S3 storage configured
- Web dashboard
- REST API server

### 2. Verify Services Are Running
```bash
# Check container status
docker-compose ps

# All containers should show "Up" status
# Expected output:
# taohash-proxy       Up  0.0.0.0:3331->3331/tcp, 0.0.0.0:3332->3332/tcp
# taohash-api         Up  0.0.0.0:8888->8000/tcp
# taohash-clickhouse  Up  0.0.0.0:8123->8123/tcp

# Check logs for any errors
docker-compose logs -f --tail=50
```

### 3. Test Connectivity
```bash
# Test API health endpoint
curl http://localhost:8888/health

# Test dashboard
curl -I http://localhost:5000

# Test if proxy ports are listening
netstat -tlnp | grep -E '3331|3332'

# Or on macOS
sudo lsof -iTCP:3331,3332 -sTCP:LISTEN
```

## Public Ports

The following ports need to be accessible:

| Port | Service | Purpose | Expose to |
|------|---------|---------|-----------|
| **3331** | Mining proxy (normal) | Standard difficulty mining | Miners only |
| **3332** | Mining proxy (high) | High difficulty mining | Miners only |
| **5000** | Web Dashboard | Monitoring interface | As needed (pref Internal/VPN) |
| **8888** | REST API | Statistics API (auth required) | As needed (pref Internal/VPN) |

### Firewall Configuration

#### AWS EC2 Security Group Configuration

1. **Via AWS Console:** 
   - Navigate to EC2 → Security Groups
   - Select your instance's security group
   - Click "Edit inbound rules"
   - Add rules to allow mining ports and dashboard/API ports. 

**Security Note:** Mining ports (3331/3332) must be open to all IPs for miners to connect. Dashboard and API ports should be restricted to trusted IPs only. 

2. **Local Server (Ubuntu/Debian with ufw)**
```bash
sudo ufw allow 3331/tcp comment "TaoHash normal diff"
sudo ufw allow 3332/tcp comment "TaoHash high diff"
sudo ufw allow 5000/tcp comment "TaoHash dashboard"
sudo ufw allow 8888/tcp comment "TaoHash API"
```

### IMPORTANT: Static IP (Required)

- If you are a validator and using EC2 to host your proxy, you need to make sure that you allocate an elastic IP and associate it with your instance. 
- This is mandatory as the miners require a static address to connect to your pools. 
- Not doing so will disrupt operations and you will stop recieving hashrate.

#### Elastic IP allocation
- Navigate to `Elastic IPs` through the sidebar in your instance
- Select `Allocate Elastic IP address` and complete the setup. 
- Select the created IP and allocate it to your instance. This address will be used when adding commitments. 

## Configuration

### Mining Proxy Settings

Edit `config/config.toml` to configure the upstream pool addresses.
Make sure you add support for both, high difficulty and low difficulty miners, even if you are using Braiins pool. 

You can use `config.toml.example` as reference. 

### Environment Variables

Mandatory variables to be updated `.env`:
- AWS credentials for S3 storage (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_S3_BUCKET)
- ClickHouse password (CLICKHOUSE_PASSWORD)
- API tokens (API_TOKENS)

You can also change the default ports if you wish. Make sure that the pool addresses that you add to the commitment matches 1-1 with the proxy you host. 

### Testing Your Connection

There is a shell script present in this same directory which tests the proxy connection. 
After setting up the proxy, you can execute it to make sure ports are listening. 

`chmod +x test_proxy.sh`
`./test_proxy.sh`

## Monitoring

### Web Dashboard (Port 5000)

The dashboard provides real-time monitoring of your mining operation:

1. **Access Dashboard**
   ```
   http://localhost:5000/
   ```
   To access it outside of your local machine, make sure that the port is accepting connections and replace `localhost` with your machine's IP address. 

2. **Dashboard Features**
   - **Active Miners**: List of connected miners with:
     - Worker names and IP addresses
     - Current hashrate (5-minute average)
     - Accepted/rejected share counts
     - Share difficulty information
     - Rejection breakdown by type (hover over the rejection number to see stats)
   
   - **Pool Statistics**: 
     - Connection status to upstream pools
     - Pool-wide hashrate
     - Total shares submitted

### Analytics Dashboard (Port 5000/analytics)

Advanced historical data analysis:

1. **Access Analytics**
   ```
   http://localhost:5000/analytics
   ```

2. **Features**
   - Query historical share data
   - Filter by date range, worker, pool
   - Export data for external analysis

### API Monitoring (Port 8888)

Programmatic access to statistics:

```bash
# Get current stats
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:8888/api/pool/stats | jq .

# Get specific pool stats
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:8888/api/pool/stats?pool=normal | jq .

# Get worker details
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:8888/api/workers/stats | jq .

  # Get worker details for a specific pool
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:8888/api/workers/stats?pool=high_diff | jq .
```

## Troubleshooting

### Common Issues and Solutions

#### **Miners Can't Connect**
```bash
# 1. Check if proxy is listening
netstat -tlnp | grep -E '3331|3332'

# 2. Test connectivity from miner location
telnet your-proxy-ip 3331
```

#### **High Rejection Rate**
```bash
# 1. Check proxy logs for rejection reasons
docker compose logs proxy | grep -i reject

# 2. Verify pool configuration
docker exec taohash-proxy cat /app/config/config.toml
```

#### **ClickHouse/S3 Issues**
```bash
# 1. Check ClickHouse status
docker compose logs clickhouse | tail -50

# 2. Test S3 connectivity
docker exec taohash-clickhouse clickhouse-client -q "SELECT * FROM system.disks"

# 3. Check disk space
df -h
docker system df
```

#### **Dashboard Not Loading**
```bash
# 1. Check if API is responding
curl http://localhost:8888/health

# 2. Verify dashboard container
docker compose ps | grep proxy

# 3. Check browser console for errors (F12)

# 4. Test API authentication
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:8888/api/pool/stats
```

### Debug Mode

Enable detailed logging:

1. **Edit docker-compose.yml**
   ```yaml
   environment:
     - LOG_LEVEL=DEBUG
   ```

2. **View detailed logs**
   ```bash
   docker compose logs -f --tail=1000
   ```

3. **Filter specific components**
   ```bash
   # Just miner connections
   docker compose logs -f | grep MinerSession
   
   # Just share submissions  
   docker compose logs -f | grep "share\|Share"
   ```

## Network Security

- NEVER expose the `5001` port which is used to update the proxy with updated pool information in the config file. 
- You may also add specific IPs who can access the dashboard. 

## Next Steps

- [API Documentation](./API.md) - REST API endpoints and usage
