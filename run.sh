#!/usr/bin/env bash

set -euo pipefail

usage() {
    echo "Usage: $0 <env_file> <data_path> [host]"
    echo ""
    echo "  env_file   Path to .env file with API keys and tokens"
    echo "  data_path  Path to persistent data directory (on the target host)"
    echo "  host       Optional SSH host to deploy to (e.g. docks.local)"
    echo ""
    echo "If a bot_ross container is already running it will be stopped and replaced."
    exit 1
}

if [ -z "${2:-}" ]; then
    usage
fi

ENV_FILE=$1
DATA_PATH=$2
HOST=${3:-}

if [ -n "$HOST" ]; then
    export DOCKER_HOST="ssh://$HOST"
    echo "Deploying to $HOST..."
fi

if docker ps -a --format '{{.Names}}' | grep -q '^bot_ross$'; then
    echo "Stopping existing bot_ross container..."
    docker stop bot_ross
    docker rm bot_ross
fi

echo "Starting bot_ross..."
docker run -d --restart=unless-stopped \
    -v "$DATA_PATH:/app/data" \
    --env-file "$ENV_FILE" \
    --name bot_ross \
    bot_ross
echo "bot_ross is running."
