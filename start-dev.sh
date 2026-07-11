#!/bin/bash
# Dev deployment wrapper — stands up dev-router-pod on distinct ports alongside prod
set -e
WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$WORKDIR"

export DEV_ENV_FILE="${WORKDIR}/.env.dev"

exec bash start-stack.sh "$@"
