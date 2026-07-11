#!/bin/bash
# Dev deployment wrapper — stands up dev-router-pod on distinct ports alongside prod
set -e
WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$WORKDIR"

export DEV_ENV_FILE="${WORKDIR}/.env.dev"
export DATA_ROOT="${WORKDIR}/dev-data"

mkdir -p dev-data/valkey-data dev-data/postgres-data dev-data/clickhouse-data \
         dev-data/redis-lf-data dev-data/minio-data dev-data/clickhouse-config \
         dev-data/litellm-rendered

exec bash start-stack.sh "$@"
