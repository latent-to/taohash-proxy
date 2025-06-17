#!/bin/bash

[ -f .env ] && source .env

# Use defaults if not set
PROXY_PORT=${PROXY_PORT:-3331}
PROXY_PORT_HIGH=${PROXY_PORT_HIGH:-3332}

echo "Testing proxy miner ports..."
echo "Normal Port: $PROXY_PORT, High Diff Port: $PROXY_PORT_HIGH"
echo

test_port() {
    local port=$1
    local label=$2
    
    echo -n "Testing $label (port $port)... "
    
    if echo '{"id": 1, "method": "mining.subscribe", "params": []}' | nc -w 3 localhost $port >/dev/null 2>&1; then
        echo "✓ OK"
    else
        echo "✗ Failed"
    fi
}

test_port $PROXY_PORT "Normal Difficulty"
test_port $PROXY_PORT_HIGH "High Difficulty"

echo
echo "Test complete!"