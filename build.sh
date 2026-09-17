#!/usr/bin/env bash

set -euo pipefail

HOST=${1:-}

if [ -n "$HOST" ]; then
    export DOCKER_HOST="ssh://$HOST"
    echo "Building on $HOST..."
fi

# Build the test stage FIRST. It runs the full local unit-test gate against the
# exact interpreter and wheels the runtime image ships; under `set -e` a failing
# suite aborts here, so the bot_ross tag below structurally cannot be produced
# from an image whose tests do not pass. --progress=plain keeps the unittest
# "Ran N tests ... OK" summary visible in the build log WHEN THE LAYER ACTUALLY
# RUNS. On an unchanged build context the RUN layer is a BuildKit cache hit and
# the log shows "CACHED" instead, with no "Ran N tests" line -- that's expected,
# not a silent skip: the cache key covers every file the tests can see, so a
# cache hit means this exact content already passed.
echo "Running the test suite inside the image (docker build --target test)..."
docker build --progress=plain --target test -t bot_ross-test .

# The runtime image is the base stage only -- the test stage (last in the
# Dockerfile, so what a bare `docker build` would produce) carries test files
# that must not ship.
echo "Tests passed. Building the runtime image..."
docker build --target base -t bot_ross .
