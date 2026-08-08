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

# Grace period for `docker stop` to let the bot drain in-flight image generations before
# it's force-killed. Must exceed the bot's DRAIN_TIMEOUT (default 300s -- raised so a
# full 5-segment pipe chain, bracketed as one drain unit, has room to finish) so the bot
# exits on its own first; when idle the bot closes immediately and stop returns right away.
STOP_TIMEOUT=${STOP_TIMEOUT:-330}

if [ -n "$HOST" ]; then
    export DOCKER_HOST="ssh://$HOST"
    echo "Deploying to $HOST..."
fi

if docker ps -a --format '{{.Names}}' | grep -q '^bot_ross$'; then
    echo "Stopping existing bot_ross container (draining in-flight requests, up to ${STOP_TIMEOUT}s)..."
    docker stop -t "$STOP_TIMEOUT" bot_ross
    docker rm bot_ross
fi

echo "Starting bot_ross..."
docker run -d --restart=unless-stopped \
    -v "$DATA_PATH:/app/data" \
    --env-file "$ENV_FILE" \
    --name bot_ross \
    bot_ross
echo "bot_ross is running."
