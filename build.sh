#!/usr/bin/env bash

set -euo pipefail

HOST=${1:-}

if [ -n "$HOST" ]; then
    export DOCKER_HOST="ssh://$HOST"
    echo "Building on $HOST..."
fi

docker build -t bot_ross .
