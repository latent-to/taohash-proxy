#!/bin/bash
# Manually trigger snapshot collection

# Check if running in Docker or locally
if [ -f /.dockerenv ]; then
    echo "Running in Docker environment"
    CONTAINER=""
else
    echo "Running locally - will execute in Docker container"
    CONTAINER="docker exec taohash-snapshot-cron"
fi

# Parse arguments
if [ "$1" == "--yesterday" ] || [ -z "$1" ]; then
    echo "Capturing yesterday's snapshot..."
    $CONTAINER python /app/scripts/daily_snapshot.py --yesterday
elif [ "$1" == "--date" ] && [ -n "$2" ]; then
    echo "Capturing snapshot for $2..."
    $CONTAINER python /app/scripts/daily_snapshot.py --date "$2"
elif [ "$1" == "--backfill" ] && [ -n "$2" ]; then
    echo "Backfilling range $2..."
    $CONTAINER python /app/scripts/daily_snapshot.py --backfill "$2"
else
    echo "Usage:"
    echo "  $0                    # Capture yesterday"
    echo "  $0 --date YYYY-MM-DD  # Capture specific date"
    echo "  $0 --backfill START:END # Backfill date range"
    exit 1
fi