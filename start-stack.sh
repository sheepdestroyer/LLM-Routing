#!/bin/bash
set -e

# Usage:
#   ./start-stack.sh              → Restart existing pod (fast, preserves logs)
#   ./start-stack.sh --replace    → Graceful stop + clean ports + redeploy pod
#                                    (for pod.yaml changes: ports, probes, env vars)
#   ./start-stack.sh --full-rebuild → Same as --replace + rebuild router image
#                                      (for router/Dockerfile changes)

# Set working directory
WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$WORKDIR"

show_help() {
    echo "Usage:"
    echo "  ./start-stack.sh                   → Restart existing pod (fast, preserves logs)"
    echo "  ./start-stack.sh --replace         → Stop, clean up zombie ports, and recreate/redeploy pod"
    echo "  ./start-stack.sh --pull            → Pull latest router image from GHCR and recreate/redeploy pod"
    echo "  ./start-stack.sh --full-rebuild    → Rebuild custom router image locally and recreate/redeploy pod"
    echo "  ./start-stack.sh --help | -h       → Show this help message and exit"
}

escape_env_val() {
    local val="$1"
    val="${val//\\/\\\\}"
    val="${val//\"/\\\"}"
    val="${val//\$/\\\$}"
    val="${val//\`/\\\`}"
    printf '%s\n' "$val"
}


if [ $# -gt 1 ]; then
    echo "❌ Error: Too many arguments supplied (expected at most 1, got $#)"
    show_help
    exit 1
fi

PULL_MODE=false
FULL_REBUILD=false
REPLACE_MODE=false
if [ "${1:-}" = "--pull" ]; then
    PULL_MODE=true
elif [ "${1:-}" = "--full-rebuild" ]; then
    FULL_REBUILD=true
elif [ "${1:-}" = "--replace" ]; then
    REPLACE_MODE=true
elif [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    show_help
    exit 0
elif [ -n "${1:-}" ]; then
    echo "❌ Error: Unknown argument '${1}'"
    show_help
    exit 1
fi



ENV_FILE="${WORKDIR}/.env"

# Ensure the env file exists and has secure permissions (owner read/write only)
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

# 1. Load or prompt for OpenRouter API Key
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# Load optional dev-environment overlay (set DEV_ENV_FILE before calling this script)
if [ -n "${DEV_ENV_FILE:-}" ] && [ -f "$DEV_ENV_FILE" ]; then
    set -a
    source "$DEV_ENV_FILE"
    set +a
fi

# Port assignments — read from env (set by .env or .env.dev) with prod defaults
POD_NAME="${POD_NAME:-prod-router-pod}"
ROUTER_PORT="${ROUTER_PORT:-5000}"
LITELLM_PORT="${LITELLM_PORT:-4000}"
LANGFUSE_WEB_PORT="${LANGFUSE_WEB_PORT:-3001}"
LANGFUSE_WORKER_PORT="${LANGFUSE_WORKER_PORT:-3030}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
VALKEY_CACHE_PORT="${VALKEY_CACHE_PORT:-6379}"
VALKEY_LF_PORT="${VALKEY_LF_PORT:-6380}"
CLICKHOUSE_HTTP_PORT="${CLICKHOUSE_HTTP_PORT:-8123}"
CLICKHOUSE_TCP_PORT="${CLICKHOUSE_TCP_PORT:-9000}"
CLICKHOUSE_INTERSERVER_PORT="${CLICKHOUSE_INTERSERVER_PORT:-9009}"
MINIO_S3_PORT="${MINIO_S3_PORT:-9002}"
MINIO_CONSOLE_PORT="${MINIO_CONSOLE_PORT:-9001}"
ROUTER_IMAGE="${ROUTER_IMAGE:-ghcr.io/sheepdestroyer/llm-routing:latest}"
DATA_ROOT="${DATA_ROOT:-${WORKDIR}/data}"
export POD_NAME ROUTER_PORT LITELLM_PORT LANGFUSE_WEB_PORT LANGFUSE_WORKER_PORT POSTGRES_PORT VALKEY_CACHE_PORT VALKEY_LF_PORT CLICKHOUSE_HTTP_PORT CLICKHOUSE_TCP_PORT CLICKHOUSE_INTERSERVER_PORT MINIO_S3_PORT MINIO_CONSOLE_PORT ROUTER_IMAGE DATA_ROOT

# Ensure local volume directories exist on the host for Podman mounts
mkdir -p "${DATA_ROOT}/valkey-data" "${DATA_ROOT}/postgres-data" "${DATA_ROOT}/langfuse-data" "${DATA_ROOT}/clickhouse-data" "${DATA_ROOT}/redis-lf-data" "${DATA_ROOT}/minio-data"

# Define and export the routing domain
ROUTING_DOMAIN="${ROUTING_DOMAIN:-vendeuvre.lan}"
export ROUTING_DOMAIN

# Derive public/local base URLs from env/config with sensible defaults, removing trailing slash
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-${BASE_URL:-${BASEURL:-https://x570.${ROUTING_DOMAIN}/llm-routing}}}"
if [[ ! "$PUBLIC_BASE_URL" =~ ^https?:// ]]; then
    PUBLIC_BASE_URL="https://${PUBLIC_BASE_URL}"
fi
if [[ ! "$PUBLIC_BASE_URL" =~ /llm-routing ]]; then
    PUBLIC_BASE_URL="${PUBLIC_BASE_URL%/}/llm-routing"
fi
PUBLIC_BASE_URL="${PUBLIC_BASE_URL%/}"
LOCAL_BASE_URL="${LOCAL_BASE_URL:-http://localhost:${ROUTER_PORT}}"
LOCAL_BASE_URL="${LOCAL_BASE_URL%/}"
export PUBLIC_BASE_URL LOCAL_BASE_URL


# Ensure openssl is installed if we need to generate passwords/keys
if [ -z "$POSTGRES_PASSWORD" ] || [ -z "$NEXTAUTH_SECRET" ] || [ -z "$SALT" ] || [ -z "$ENCRYPTION_KEY" ] || [ -z "$LITELLM_MASTER_KEY" ] || [ -z "$ROUTER_API_KEY" ] || [ -z "$MINIO_ROOT_USER" ] || [ -z "$MINIO_ROOT_PASSWORD" ] || [ -z "$LANGFUSE_INIT_USER_PASSWORD" ] || [ -z "$REDIS_AUTH" ] || [ -z "$CLICKHOUSE_PASSWORD" ] || [ -z "$LANGFUSE_PUBLIC_KEY" ] || [ -z "$LANGFUSE_SECRET_KEY" ]; then
    if ! command -v openssl &>/dev/null; then
        echo "❌ Error: 'openssl' is required to generate secure random keys but was not found in PATH."
        exit 1
    fi
fi

if [ -z "$OPENROUTER_API_KEY" ]; then
    if [ -t 0 ]; then
        echo "🔑 OpenRouter API Key not found."
        while [ -z "$OPENROUTER_API_KEY" ]; do
            echo -n "Please enter your OpenRouter API Key (input will be hidden): "
            if ! read -rs OPENROUTER_API_KEY; then
                echo -e "\n❌ Error: Failed to read OpenRouter API Key (EOF reached). Aborting." >&2
                exit 1
            fi
            echo ""
            if [ -z "$OPENROUTER_API_KEY" ]; then
                echo "❌ Error: API key cannot be empty. Please try again."
            fi
        done
        escaped_key=$(escape_env_val "$OPENROUTER_API_KEY")
        echo "OPENROUTER_API_KEY=\"$escaped_key\"" >> "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        echo "✓ API key saved securely to $ENV_FILE"
    else
        echo "❌ Error: OPENROUTER_API_KEY is not set in your environment or in $ENV_FILE"
        echo "Please run this script interactively first, or create the file manually:"
        echo "  echo 'OPENROUTER_API_KEY=your_key_here' > $ENV_FILE"
        echo "  chmod 600 $ENV_FILE"
        exit 1
    fi
fi

if [ -z "$POSTGRES_PASSWORD" ]; then
    echo "🔐 Generating secure POSTGRES_PASSWORD..."
    POSTGRES_PASSWORD=$(openssl rand -hex 16)
    echo "POSTGRES_PASSWORD=\"$POSTGRES_PASSWORD\"" >> "$ENV_FILE"
    chmod 600 "$ENV_FILE"
fi

# 2. Sync Gemini OAuth token (skip if <15 min old)
OAUTH_CREDS="$HOME/.gemini/oauth_creds.json"
NEED_SYNC=true
if [ -f "$OAUTH_CREDS" ]; then
    CREDS_AGE=$(($(date +%s) - $(stat -c %Y "$OAUTH_CREDS" 2>/dev/null || echo 0)))
    if [ "$CREDS_AGE" -lt 900 ]; then
        NEED_SYNC=false
    fi
fi
if $NEED_SYNC; then
    python3 scripts/sync_gemini_token.py || echo "⚠️ Warning: Failed to sync Gemini token from keyring"
fi

ACTIVE_OAUTH=""
if [ -f "$OAUTH_CREDS" ]; then
    ACTIVE_OAUTH=$(jq -r '.access_token' "$OAUTH_CREDS" 2>/dev/null || echo "")
fi
if [ -z "$ACTIVE_OAUTH" ]; then
    echo "⚠️ Warning: Could not resolve Google OAuth token from $OAUTH_CREDS."
    echo "Gemini models may fail. Please ensure you are logged into Antigravity."
fi

# Check host agy daemon
if systemctl --user is-active --quiet agy-daemon.service 2>/dev/null; then
    echo "✓ Host agy daemon is running"
else
    echo "⚠️  Warning: Host agy daemon is not running. Starting it..."
    systemctl --user start agy-daemon.service || echo "⚠️  Failed to start agy daemon"
fi

# Verify daemon is responsive
if curl -s --max-time 2 http://127.0.0.1:5005/run >/dev/null 2>&1; then
    echo "✓ Host agy daemon responsive on port 5005"
else
    echo "⚠️  Warning: Host agy daemon not responding on port 5005"
fi

# Ensure the env file exists and has secure permissions (owner read/write only)
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

gen_hex() {
    local val
    val=$(openssl rand -hex "$1" 2>/dev/null)
    local status=$?
    local expected_len=$(( $1 * 2 ))
    if [ $status -ne 0 ] || [ ${#val} -ne $expected_len ]; then
        echo "❌ Error: Failed to generate secure random hex value of byte length $1 (openssl rand exit $status, length ${#val})." >&2
        return 1
    fi
    printf '%s' "$val"
}

gen_base64() {
    local val
    val=$(openssl rand -base64 "$1" 2>/dev/null)
    local status=$?
    if [ $status -ne 0 ] || [ -z "$val" ]; then
        echo "❌ Error: Failed to generate secure random base64 value of byte length $1 (openssl rand exit $status)." >&2
        return 1
    fi
    printf '%s' "$val"
}

generate_uuid() {
    local val
    val=$(gen_hex 16) || return 1
    echo "${val:0:8}-${val:8:4}-${val:12:4}-${val:16:4}-${val:20:12}"
}

if [ -z "$NEXTAUTH_SECRET" ]; then
    NEXTAUTH_SECRET="$(gen_base64 32)" || exit 1
    echo "NEXTAUTH_SECRET=\"$NEXTAUTH_SECRET\"" >> "$ENV_FILE"
    echo "✓ Generated new NEXTAUTH_SECRET and saved to $ENV_FILE"
fi

if [ -z "$SALT" ]; then
    SALT="$(gen_hex 32)" || exit 1
    echo "SALT=\"$SALT\"" >> "$ENV_FILE"
    echo "✓ Generated new SALT and saved to $ENV_FILE"
fi

if [ -z "$ENCRYPTION_KEY" ]; then
    ENCRYPTION_KEY="$(gen_hex 32)" || exit 1
    echo "ENCRYPTION_KEY=\"$ENCRYPTION_KEY\"" >> "$ENV_FILE"
    echo "✓ Generated new ENCRYPTION_KEY and saved to $ENV_FILE"
fi

if [ -z "$LITELLM_MASTER_KEY" ]; then
    rand_key="$(gen_hex 16)" || exit 1
    LITELLM_MASTER_KEY="sk-litellm-$rand_key"
    echo "LITELLM_MASTER_KEY=\"$LITELLM_MASTER_KEY\"" >> "$ENV_FILE"
    echo "✓ Generated new LiteLLM master key and saved to $ENV_FILE"
fi

if [ -z "$LITELLM_MASTER_KEY" ]; then
    echo "❌ Error: LITELLM_MASTER_KEY is not set and could not be generated."
    exit 1
fi

if [ -z "$LANGFUSE_INIT_USER_PASSWORD" ]; then
    LANGFUSE_INIT_USER_PASSWORD="$(gen_hex 16)" || exit 1
    echo "LANGFUSE_INIT_USER_PASSWORD=\"$LANGFUSE_INIT_USER_PASSWORD\"" >> "$ENV_FILE"
    echo "✓ Generated new LANGFUSE_INIT_USER_PASSWORD and saved to $ENV_FILE"
fi

if [ -z "$REDIS_AUTH" ]; then
    REDIS_AUTH="$(gen_hex 16)" || exit 1
    echo "REDIS_AUTH=\"$REDIS_AUTH\"" >> "$ENV_FILE"
    echo "✓ Generated new REDIS_AUTH and saved to $ENV_FILE"
fi

if [ -z "$CLICKHOUSE_PASSWORD" ]; then
    CLICKHOUSE_PASSWORD="$(gen_hex 16)" || exit 1
    echo "CLICKHOUSE_PASSWORD=\"$CLICKHOUSE_PASSWORD\"" >> "$ENV_FILE"
    echo "✓ Generated new CLICKHOUSE_PASSWORD and saved to $ENV_FILE"
fi

if [ -z "$ROUTER_API_KEY" ]; then
    ROUTER_API_KEY="$(gen_hex 32)" || exit 1
    echo "ROUTER_API_KEY=\"$ROUTER_API_KEY\"" >> "$ENV_FILE"
    echo "✓ Generated new ROUTER_API_KEY and saved to $ENV_FILE"
fi

if [ -z "$MINIO_ROOT_USER" ]; then
    rand_user="$(gen_hex 4)" || exit 1
    MINIO_ROOT_USER="minio-$rand_user"
    echo "MINIO_ROOT_USER=\"$MINIO_ROOT_USER\"" >> "$ENV_FILE"
    echo "✓ Generated new MINIO_ROOT_USER and saved to $ENV_FILE"
fi

if [ -z "$MINIO_ROOT_PASSWORD" ]; then
    MINIO_ROOT_PASSWORD="$(gen_hex 16)" || exit 1
    echo "MINIO_ROOT_PASSWORD=\"$MINIO_ROOT_PASSWORD\"" >> "$ENV_FILE"
    echo "✓ Generated new MINIO_ROOT_PASSWORD and saved to $ENV_FILE"
fi

if [ -z "$LANGFUSE_PUBLIC_KEY" ]; then
    if ! uuid=$(generate_uuid) || [ -z "$uuid" ]; then
        echo "❌ Error: Failed to generate LANGFUSE_PUBLIC_KEY." >&2
        exit 1
    fi
    LANGFUSE_PUBLIC_KEY="pk-lf-$uuid"
    echo "LANGFUSE_PUBLIC_KEY=\"$LANGFUSE_PUBLIC_KEY\"" >> "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "✓ Generated new LANGFUSE_PUBLIC_KEY and saved to $ENV_FILE"
fi

if [ -z "$LANGFUSE_SECRET_KEY" ]; then
    if ! uuid=$(generate_uuid) || [ -z "$uuid" ]; then
        echo "❌ Error: Failed to generate LANGFUSE_SECRET_KEY." >&2
        exit 1
    fi
    LANGFUSE_SECRET_KEY="sk-lf-$uuid"
    echo "LANGFUSE_SECRET_KEY=\"$LANGFUSE_SECRET_KEY\"" >> "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "✓ Generated new LANGFUSE_SECRET_KEY and saved to $ENV_FILE"
fi

if [ -z "$OLLAMA_API_KEY" ]; then
    if [ -t 0 ]; then
        echo "🔑 OLLAMA_API_KEY not found."
        while [ -z "$OLLAMA_API_KEY" ]; do
            echo -n "Please enter your Ollama API Key (input will be hidden): "
            if ! read -rs OLLAMA_API_KEY; then
                echo -e "\n❌ Error: Failed to read Ollama API Key (EOF reached). Aborting." >&2
                exit 1
            fi
            echo ""
            if [ -z "$OLLAMA_API_KEY" ]; then
                echo "❌ Error: API key cannot be empty. Please try again."
            fi
        done
        escaped_key=$(escape_env_val "$OLLAMA_API_KEY")
        echo "OLLAMA_API_KEY=\"$escaped_key\"" >> "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        echo "✓ Ollama API key saved securely to $ENV_FILE"
    else
        echo "❌ Error: OLLAMA_API_KEY is not set in your environment or in $ENV_FILE."
        echo "Please run this script interactively first, or create the file manually:"
        echo "  echo 'OLLAMA_API_KEY=your_key_here' >> $ENV_FILE"
        echo "  chmod 600 $ENV_FILE"
        exit 1
    fi
fi

if [ -z "$CLASSIFIER_INPUT_MAX_CHARS" ]; then
    CLASSIFIER_INPUT_MAX_CHARS="300"
    echo "CLASSIFIER_INPUT_MAX_CHARS=\"$CLASSIFIER_INPUT_MAX_CHARS\"" >> "$ENV_FILE"
    echo "✓ Set default CLASSIFIER_INPUT_MAX_CHARS=300 and saved to $ENV_FILE"
fi




# DYNAMIC_LITELLM_MASTER_KEY_PLACEHOLDER in router config is resolved at runtime from env

# Arguments parsed at top of script

# ── Cleanup zombie host-network ports ──
# Podman with host networking can leave stuck LISTEN sockets after SIGKILL.
# This covers ALL ports used by the pod + cross-profile orphans from other
# Hermes profiles (e.g., llm-routing-openrouter) whose container storage
# can leave surviving processes holding ports indefinitely.
cleanup_zombie_ports() {
    local ALL_PORTS="$ROUTER_PORT $LITELLM_PORT $LANGFUSE_WEB_PORT $LANGFUSE_WORKER_PORT $POSTGRES_PORT $VALKEY_CACHE_PORT $VALKEY_LF_PORT $CLICKHOUSE_HTTP_PORT $CLICKHOUSE_TCP_PORT $CLICKHOUSE_INTERSERVER_PORT $MINIO_S3_PORT $MINIO_CONSOLE_PORT 8080 9004 9005"
    
    echo "🧹 Cleaning up zombie port bindings..."
    
    # Pass 1: fuser kill for processes still alive (works on same-profile zombies)
    for port in $ALL_PORTS; do
        local pid=$(fuser "${port}/tcp" 2>/dev/null)
        if [ -n "$pid" ]; then
            echo "   Killing PID $pid on port $port"
            kill -9 $pid 2>/dev/null || true
        fi
    done
    
    # Pass 2: ss-based detection for orphaned sockets with no PID (kernel zombies)
    # and cross-profile orphans that fuser may miss
    sleep 2
    local stuck_ports=""
    for port in $ALL_PORTS; do
        if ss -tlnpH 2>/dev/null | grep -q ":${port} "; then
            stuck_ports="$stuck_ports $port"
        fi
    done
    
    if [ -z "$stuck_ports" ]; then
        echo "   ✓ All ports clean after Pass 1"
        return 0
    fi
    
    echo "   ⚠️  Ports still bound after fuser: $stuck_ports"
    echo "   🔍 Checking for cross-profile orphans..."
    
    # Pass 3: find ANY process listening on our ports, kill by PID via ss
    for port in $ALL_PORTS; do
        while IFS= read -r line; do
            local pid=$(echo "$line" | grep -oP 'pid=\K\d+')
            if [ -n "$pid" ]; then
                local proc_name=$(ps -p $pid -o comm= 2>/dev/null || echo "unknown")
                echo "   Killing cross-profile orphan: $proc_name (PID $pid) on port $port"
                kill -9 $pid 2>/dev/null || true
            fi
        done < <(ss -tlnpH 2>/dev/null | grep ":${port} " | grep -oP 'pid=\d+')
    done
    
    # Pass 4: wait up to 60s for kernel to release orphaned sockets
    local waited=0
    while [ $waited -lt 60 ]; do
        local still_stuck=0
        for port in $ALL_PORTS; do
            if ss -tlnpH 2>/dev/null | grep -q ":${port} "; then
                still_stuck=$((still_stuck + 1))
            fi
        done
        if [ "$still_stuck" -eq 0 ]; then
            echo "   ✓ All ports released after ${waited}s"
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
    
    local final=$(ss -tlnpH 2>/dev/null | grep -cE ":(${ALL_PORTS// /|})") || true
    echo "   ⚠️  Warning: ${final:-0} zombie port(s) survived 60s cleanup wait"
}

# ── MinIO bucket auto-creation ──
# Ensures required S3 buckets exist before Langfuse attempts uploads.
# Buckets are persisted via hostPath volume (minio-data/) across restarts.
setup_minio_buckets() {
    local MAX_WAIT=60
    local waited=0

    echo ""
    echo "📦 Ensuring MinIO buckets exist..."

    # Wait for MinIO S3 API to be ready
    while [ $waited -lt $MAX_WAIT ]; do
        if curl -sf --max-time 3 http://127.0.0.1:${MINIO_S3_PORT}/minio/health/live >/dev/null 2>&1; then
            echo "   ✓ MinIO S3 API ready after ${waited}s"
            break
        fi
        sleep 3
        waited=$((waited + 3))
    done
    if [ $waited -ge $MAX_WAIT ]; then
        echo "   ⚠️  MinIO not ready after ${MAX_WAIT}s — skipping bucket creation"
        return 1
    fi

    # Ensure mc alias points to the correct MinIO S3 API port
    # The default 'local' alias in the MinIO image points to :9000 which is ClickHouse,
    # not MinIO. We must override it.
    if ! podman exec ${POD_NAME}-minio-s3 mc alias set local http://127.0.0.1:${MINIO_S3_PORT} "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"; then
        echo "❌ Error: Failed to set MinIO alias 'local' on http://127.0.0.1:${MINIO_S3_PORT}" >&2
        exit 1
    fi

    # Create required buckets (idempotent)
    local BUCKETS=("langfuse-events" "proj-triage-gateway-id")
    for bucket in "${BUCKETS[@]}"; do
        if podman exec ${POD_NAME}-minio-s3 mc ls "local/${bucket}" >/dev/null 2>&1; then
            echo "   ✓ Bucket '${bucket}' exists"
        else
            echo "   + Creating bucket '${bucket}'..."
            podman exec ${POD_NAME}-minio-s3 mc mb "local/${bucket}" 2>/dev/null || {
                echo "   ⚠️  Failed to create bucket '${bucket}'"
            }
        fi
    done
}

# ── Post-deploy health verification ──
# Waits for critical services to become healthy and verifies the
# full routing pipeline works through the entry point.
verify_stack_health() {
    local MAX_WAIT=180
    local waited=0
    
    echo ""
    echo "🩺 Verifying stack health (up to ${MAX_WAIT}s)..."
    
    # Wait for postgres first — everything depends on it
    while [ $waited -lt $MAX_WAIT ]; do
        if podman exec ${POD_NAME}-postgres-db pg_isready -U postgres -p ${POSTGRES_PORT} -q 2>/dev/null; then
            echo "   ✓ PostgreSQL ready after ${waited}s"
            break
        fi
        sleep 5
        waited=$((waited + 5))
    done
    if [ $waited -ge $MAX_WAIT ]; then
        echo "   ⚠️  PostgreSQL not ready after ${MAX_WAIT}s"
        return 1
    fi
    
    # Wait for LiteLLM (Prisma migrate can take 2-3 min on fresh DB)
    local litellm_ready=false
    waited=0
    while [ $waited -lt $MAX_WAIT ]; do
        if curl -sf --max-time 3 http://127.0.0.1:${LITELLM_PORT}/health/readiness >/dev/null 2>&1; then
            echo "   ✓ LiteLLM ready after ${waited}s"
            litellm_ready=true
            break
        fi
        sleep 5
        waited=$((waited + 5))
    done
    if ! $litellm_ready; then
        echo "   ⚠️  LiteLLM not ready after ${MAX_WAIT}s — continuing anyway"
    fi
    
    # Wait for triage router + verify full pipeline
    waited=0
    while [ $waited -lt 120 ]; do
        local resp=$(curl -s --max-time 10 http://127.0.0.1:${ROUTER_PORT}/v1/chat/completions \
            -H 'Content-Type: application/json' \
            -d '{"model":"agent-simple-core","messages":[{"role":"user","content":"Hi"}],"max_tokens":5}' 2>/dev/null)
        if echo "$resp" | grep -q '"choices"'; then
            echo "   ✓ Triage router pipeline verified after ${waited}s"
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
    
    echo "   ⚠️  Triage router pipeline not responding after 120s — dashboard may still work"
    return 1
}
# ── Stack ownership and teardown ──
# Keep the generated Quadlet unit name in one place: it is used for ownership
# detection, lifecycle operations, and user-facing diagnostics.
LLM_ROUTING_POD_UNIT="llm-routing-pod.service"
# Quadlet-managed pods carry PODMAN_SYSTEMD_UNIT on their infra container.
# Consult that metadata rather than inferring ownership from active state: a
# stopped or failed generated unit is still Quadlet-owned and must be reconciled
# through systemd before a replacement pod is created.
stack_ownership() {
    local infra_unit
    if podman pod exists "${POD_NAME}" 2>/dev/null; then
        infra_unit=$(podman pod inspect "${POD_NAME}" --format '{{.InfraContainerID}}' 2>/dev/null | xargs -r podman inspect --format '{{ index .Config.Labels "PODMAN_SYSTEMD_UNIT" }}' 2>/dev/null || true)
        if [[ "$infra_unit" == "$LLM_ROUTING_POD_UNIT" ]]; then
            printf 'quadlet\n'
        else
            printf 'legacy\n'
        fi
    elif systemctl --user show "$LLM_ROUTING_POD_UNIT" -p LoadState --value 2>/dev/null | grep -qxv 'not-found'; then
        printf 'quadlet\n'
    else
        printf 'absent\n'
    fi
}

require_user_systemd() {
    if ! systemctl --user show-environment >/dev/null 2>&1; then
        echo "❌ Error: the Quadlet deployment requires a reachable systemd --user manager." >&2
        echo "   Log in through a user session or restore the user D-Bus before deploying." >&2
        return 1
    fi
}

# Graceful stop (SIGTERM with 30s timeout) lets ClickHouse/Postgres flush,
# then force-remove if needed. Avoids data corruption from SIGKILL.
safe_pod_teardown() {
    local ownership
    ownership=$(stack_ownership)
    if [[ "$ownership" == "quadlet" ]]; then
        echo "🛑 Reconciling Quadlet-owned stack (unit may be active, inactive, or failed)..."
        systemctl --user stop "$LLM_ROUTING_POD_UNIT" 2>/dev/null || true
        systemctl --user reset-failed "$LLM_ROUTING_POD_UNIT" 2>/dev/null || true
        podman pod rm -f "${POD_NAME}" 2>/dev/null || true
        cleanup_zombie_ports
        echo "✓ Quadlet stack stopped, state reconciled, ports cleaned"
        return
    fi
    if [[ "$ownership" == "legacy" ]]; then
        echo "🛑 Gracefully stopping pod (SIGTERM, 30s timeout)..."
        podman pod stop -t 30 ${POD_NAME} 2>/dev/null || true
        # podman pod exists returns 0 for stopped pods too — check running state
        if podman pod inspect ${POD_NAME} --format '{{.State}}' 2>/dev/null | grep -q 'Running'; then
            echo "⚠️  Graceful stop timed out — force-removing..."
            podman pod rm -f ${POD_NAME} 2>/dev/null || true
        else
            # Already stopped, just remove
            podman pod rm ${POD_NAME} 2>/dev/null || true
        fi
        cleanup_zombie_ports
        echo "✓ Pod torn down, ports cleaned"
    fi
}

# Derive service URLs once so legacy pod rendering and Quadlet rendering cannot drift.
derive_external_service_urls() {
    local values
    values=$(python3 -c '
import os
from urllib.parse import urlparse
public = (os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
routing_domain = os.environ.get("ROUTING_DOMAIN") or "vendeuvre.lan"
parsed = urlparse(public if "://" in public else f"https://{public}")
scheme = parsed.scheme if parsed.scheme in {"http", "https"} else "https"
host = parsed.netloc or parsed.path.split("/", 1)[0] or routing_domain
print(os.environ.get("PROXY_BASE_URL") or f"{scheme}://litellm.{host}")
print(os.environ.get("NEXTAUTH_URL") or f"{scheme}://langfuse.{host}")
') || return 1
    PROXY_BASE_URL_DERIVED=${values%%$'\n'*}
    NEXTAUTH_URL_DERIVED=${values#*$'\n'}
    export PROXY_BASE_URL_DERIVED NEXTAUTH_URL_DERIVED
}

# Pre-deploy database backup (runs before any pod modification)
# Skip if pod doesn't exist (e.g., after manual cleanup)
if podman pod exists ${POD_NAME} 2>/dev/null; then
    echo "💾 Taking pre-deploy database backup..."
    bash scripts/backup.sh && echo "✓ Pre-deploy backup saved" || echo "⚠️ Pre-deploy backup skipped"
fi

# ── ClickHouse port override XML ──
# Writes a minimal config.d XML override so ClickHouse listens on the
# configured ports instead of its compiled-in defaults.
generate_clickhouse_config() {
    local config_dir="${DATA_ROOT}/clickhouse-config"
    mkdir -p "$config_dir"
    cat > "${config_dir}/port-override.xml" << EOF
<clickhouse>
    <http_port>${CLICKHOUSE_HTTP_PORT}</http_port>
    <tcp_port>${CLICKHOUSE_TCP_PORT}</tcp_port>
    <interserver_http_port>${CLICKHOUSE_INTERSERVER_PORT}</interserver_http_port>
</clickhouse>
EOF
    echo "✓ ClickHouse port config written to ${config_dir}/port-override.xml"
}

# ── LiteLLM rendered config ──
# Generates a rendered config.yaml (with port substitutions) into DATA_ROOT/litellm-rendered/
# so prod and dev each get their own copy with the correct port values.
render_litellm_config() {
    local rendered_dir="${DATA_ROOT}/litellm-rendered"
    mkdir -p "$rendered_dir"
    sed -e "s/VALKEY_CACHE_PORT_PLACEHOLDER/${VALKEY_CACHE_PORT}/g" \
        -e "s/ROUTER_PORT_PLACEHOLDER/${ROUTER_PORT}/g" \
        -e "s|LLAMA_CLASSIFIER_URL_PLACEHOLDER|${LLAMA_CLASSIFIER_URL:?LLAMA_CLASSIFIER_URL must be set in .env}|g" \
        "${WORKDIR}/litellm/config.yaml" > "${rendered_dir}/config.yaml"
    # Validate no unresolved placeholders remain
    if grep -E -q 'VALKEY_CACHE_PORT_PLACEHOLDER|ROUTER_PORT_PLACEHOLDER|LLAMA_CLASSIFIER_URL_PLACEHOLDER' "${rendered_dir}/config.yaml"; then
        echo "❌ Error: Unresolved placeholders remain in ${rendered_dir}/config.yaml" >&2
        exit 1
    fi
    chmod 644 "${rendered_dir}/config.yaml"
    # Copy entrypoint.py unchanged
    cp "${WORKDIR}/litellm/entrypoint.py" "${rendered_dir}/entrypoint.py"
    chmod 644 "${rendered_dir}/entrypoint.py"
    echo "✓ LiteLLM config rendered to ${rendered_dir}/config.yaml"
}

# ── Router rendered config ──
# Generates a rendered config.yaml (with port substitutions) into DATA_ROOT/router-rendered/
# so prod and dev each get their own copy with the correct LiteLLM port.
render_router_config() {
    local rendered_dir="${DATA_ROOT}/router-rendered"
    mkdir -p "$rendered_dir"
    sed -e "s/LITELLM_PORT_PLACEHOLDER/${LITELLM_PORT}/g" \
        "${WORKDIR}/router/config.yaml" > "${rendered_dir}/config.yaml"
    # Validate no unresolved placeholders remain
    if grep -q 'LITELLM_PORT_PLACEHOLDER' "${rendered_dir}/config.yaml"; then
        echo "❌ Error: Unresolved placeholders remain in ${rendered_dir}/config.yaml" >&2
        exit 1
    fi
    chmod 644 "${rendered_dir}/config.yaml"
    echo "✓ Router config rendered to ${rendered_dir}/config.yaml"
}

render_pod_yaml() {
    export WORKDIR HOME LITELLM_MASTER_KEY UI_USERNAME UI_PASSWORD
    export POSTGRES_PASSWORD NEXTAUTH_SECRET SALT ENCRYPTION_KEY
    export LANGFUSE_INIT_USER_PASSWORD MINIO_ROOT_USER MINIO_ROOT_PASSWORD
    export OLLAMA_API_KEY OPENROUTER_API_KEY LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY
    export CLASSIFIER_INPUT_MAX_CHARS REDIS_AUTH CLICKHOUSE_PASSWORD
    export PUBLIC_BASE_URL ROUTING_DOMAIN POD_NAME DATA_ROOT
    export LLAMA_CLASSIFIER_URL LLAMA_SERVER_URL
    export ROUTER_IMAGE ROUTER_PORT LITELLM_PORT LANGFUSE_WEB_PORT
    export LANGFUSE_WORKER_PORT POSTGRES_PORT VALKEY_CACHE_PORT VALKEY_LF_PORT
    export CLICKHOUSE_HTTP_PORT CLICKHOUSE_TCP_PORT CLICKHOUSE_INTERSERVER_PORT
    export MINIO_S3_PORT MINIO_CONSOLE_PORT
    derive_external_service_urls
    python3 - "$WORKDIR/pod.yaml" <<'PY'
import os, sys, urllib.parse, json
uid = os.getuid()
with open(sys.argv[1], "r", encoding="utf-8") as f:
    text = f.read()

def yaml_scalar(val):
    return json.dumps(val)

placeholders = [
    "WORKDIR_PLACEHOLDER",
    "HOME_PLACEHOLDER",
    "RUN_USER_PLACEHOLDER",
    "LITELLM_MASTER_KEY_PLACEHOLDER",
    "LITELLM_UI_USERNAME_PLACEHOLDER",
    "LITELLM_UI_PASSWORD_PLACEHOLDER",
    "POSTGRES_PASSWORD_RAW_PLACEHOLDER",
    "POSTGRES_PASSWORD_ENCODED_PLACEHOLDER",
    "NEXTAUTH_SECRET_PLACEHOLDER",
    "NEXTAUTH_URL_PLACEHOLDER",
    "SALT_PLACEHOLDER",
    "ENCRYPTION_KEY_PLACEHOLDER",
    "OLLAMA_API_KEY_PLACEHOLDER",
    "OPENROUTER_API_KEY_PLACEHOLDER",
    "LANGFUSE_PUBLIC_KEY_PLACEHOLDER",
    "LANGFUSE_SECRET_KEY_PLACEHOLDER",
    "MINIO_USER_PLACEHOLDER",
    "MINIO_PASSWORD_PLACEHOLDER",
    "LANGFUSE_INIT_USER_PASSWORD_PLACEHOLDER",
    "REDIS_AUTH_PLACEHOLDER",
    "CLICKHOUSE_PASSWORD_PLACEHOLDER",
    "PROXY_BASE_URL_PLACEHOLDER",
    "PUBLIC_BASE_URL_PLACEHOLDER",
    "ROUTING_DOMAIN_PLACEHOLDER",
    "POD_NAME_PLACEHOLDER",
    "DATA_ROOT_PLACEHOLDER",
    "ROUTER_PORT_PLACEHOLDER",
    "LITELLM_PORT_PLACEHOLDER",
    "LANGFUSE_WEB_PORT_PLACEHOLDER",
    "LANGFUSE_WORKER_PORT_PLACEHOLDER",
    "POSTGRES_PORT_PLACEHOLDER",
    "VALKEY_CACHE_PORT_PLACEHOLDER",
    "VALKEY_LF_PORT_PLACEHOLDER",
    "CLICKHOUSE_HTTP_PORT_PLACEHOLDER",
    "CLICKHOUSE_TCP_PORT_PLACEHOLDER",
    "MINIO_S3_PORT_PLACEHOLDER",
    "MINIO_CONSOLE_PORT_PLACEHOLDER",
    "ROUTER_IMAGE_PLACEHOLDER",
]
for ph in placeholders:
    if ph not in text:
        sys.stderr.write(f"Error: Required placeholder '{ph}' not found in pod.yaml. Ensure you are using the latest version of the template.\n")
        sys.exit(1)
text = text.replace("WORKDIR_PLACEHOLDER", os.environ["WORKDIR"])
text = text.replace("HOME_PLACEHOLDER", os.environ["HOME"])
text = text.replace("RUN_USER_PLACEHOLDER", f"/run/user/{uid}")
text = text.replace("LITELLM_MASTER_KEY_PLACEHOLDER", yaml_scalar(os.environ["LITELLM_MASTER_KEY"]))
text = text.replace("LITELLM_UI_USERNAME_PLACEHOLDER", yaml_scalar(os.environ.get("UI_USERNAME") or "admin"))
text = text.replace("LITELLM_UI_PASSWORD_PLACEHOLDER", yaml_scalar(os.environ.get("UI_PASSWORD") or os.environ.get("LITELLM_MASTER_KEY") or "admin"))
text = text.replace("POSTGRES_PASSWORD_RAW_PLACEHOLDER", yaml_scalar(os.environ["POSTGRES_PASSWORD"]))
# URL-encode the postgres password for DSN insertion
encoded_password = urllib.parse.quote(os.environ['POSTGRES_PASSWORD'], safe="")
text = text.replace("POSTGRES_PASSWORD_ENCODED_PLACEHOLDER", encoded_password)
text = text.replace("NEXTAUTH_SECRET_PLACEHOLDER", yaml_scalar(os.environ["NEXTAUTH_SECRET"]))
text = text.replace("SALT_PLACEHOLDER", yaml_scalar(os.environ["SALT"]))
text = text.replace("ENCRYPTION_KEY_PLACEHOLDER", yaml_scalar(os.environ["ENCRYPTION_KEY"]))
text = text.replace("OLLAMA_API_KEY_PLACEHOLDER", yaml_scalar(os.environ["OLLAMA_API_KEY"]))
text = text.replace("OPENROUTER_API_KEY_PLACEHOLDER", yaml_scalar(os.environ["OPENROUTER_API_KEY"]))
text = text.replace("LANGFUSE_PUBLIC_KEY_PLACEHOLDER", yaml_scalar(os.environ["LANGFUSE_PUBLIC_KEY"]))
text = text.replace("LANGFUSE_SECRET_KEY_PLACEHOLDER", yaml_scalar(os.environ["LANGFUSE_SECRET_KEY"]))
text = text.replace("MINIO_USER_PLACEHOLDER", yaml_scalar(os.environ["MINIO_ROOT_USER"]))
text = text.replace("MINIO_PASSWORD_PLACEHOLDER", yaml_scalar(os.environ["MINIO_ROOT_PASSWORD"]))
text = text.replace("LANGFUSE_INIT_USER_PASSWORD_PLACEHOLDER", yaml_scalar(os.environ["LANGFUSE_INIT_USER_PASSWORD"]))
text = text.replace("REDIS_AUTH_PLACEHOLDER", yaml_scalar(os.environ["REDIS_AUTH"]))
text = text.replace("CLICKHOUSE_PASSWORD_PLACEHOLDER", yaml_scalar(os.environ["CLICKHOUSE_PASSWORD"]))
# Service URLs are derived once by the shell wrapper for both rendering paths.
proxy_base_url = os.environ["PROXY_BASE_URL_DERIVED"]
nextauth_url = os.environ["NEXTAUTH_URL_DERIVED"]

text = text.replace("PROXY_BASE_URL_PLACEHOLDER", yaml_scalar(proxy_base_url))
text = text.replace("PUBLIC_BASE_URL_PLACEHOLDER", yaml_scalar(os.environ["PUBLIC_BASE_URL"]))
text = text.replace("NEXTAUTH_URL_PLACEHOLDER", yaml_scalar(nextauth_url))
text = text.replace("ROUTING_DOMAIN_PLACEHOLDER", yaml_scalar(os.environ["ROUTING_DOMAIN"]))
# Raw replacements (no quoting — used for pod name, data root, and integer port values)
raw_replacements = {
    "POD_NAME_PLACEHOLDER": os.environ["POD_NAME"],
    "DATA_ROOT_PLACEHOLDER": os.environ["DATA_ROOT"],
    "ROUTER_PORT_PLACEHOLDER": os.environ["ROUTER_PORT"],
    "LITELLM_PORT_PLACEHOLDER": os.environ["LITELLM_PORT"],
    "LANGFUSE_WEB_PORT_PLACEHOLDER": os.environ["LANGFUSE_WEB_PORT"],
    "LANGFUSE_WORKER_PORT_PLACEHOLDER": os.environ["LANGFUSE_WORKER_PORT"],
    "POSTGRES_PORT_PLACEHOLDER": os.environ["POSTGRES_PORT"],
    "VALKEY_CACHE_PORT_PLACEHOLDER": os.environ["VALKEY_CACHE_PORT"],
    "VALKEY_LF_PORT_PLACEHOLDER": os.environ["VALKEY_LF_PORT"],
    "CLICKHOUSE_HTTP_PORT_PLACEHOLDER": os.environ["CLICKHOUSE_HTTP_PORT"],
    "CLICKHOUSE_TCP_PORT_PLACEHOLDER": os.environ["CLICKHOUSE_TCP_PORT"],
    "MINIO_S3_PORT_PLACEHOLDER": os.environ["MINIO_S3_PORT"],
    "MINIO_CONSOLE_PORT_PLACEHOLDER": os.environ["MINIO_CONSOLE_PORT"],
    "ROUTER_IMAGE_PLACEHOLDER": os.environ["ROUTER_IMAGE"],
}
for ph, val in raw_replacements.items():
    text = text.replace(ph, val)
import re
unresolved = sorted(set(re.findall(r"\b[A-Z0-9_]+_PLACEHOLDER\b", text)))
if unresolved:
    sys.stderr.write(
        "Error: Unresolved placeholders remain in rendered pod.yaml: "
        + ", ".join(unresolved)
        + "\n"
    )
    sys.exit(1)
sys.stdout.write(text)
PY
}

# ── Quadlet rendering + installation ──
# Renders quadlets/*.pod + quadlets/*.container templates (same _PLACEHOLDER
# convention as pod.yaml) into ~/.config/containers/systemd/llm-routing/ and
# lets systemd's podman-user-generator turn them into real units.
# Quadlet values are bare scalars (not YAML) so plain string replacement is used.
QUADLET_DIR="${HOME}/.config/containers/systemd/llm-routing"

render_quadlets() {
    export WORKDIR HOME LITELLM_MASTER_KEY UI_USERNAME UI_PASSWORD
    export POSTGRES_PASSWORD NEXTAUTH_SECRET SALT ENCRYPTION_KEY
    export LANGFUSE_INIT_USER_PASSWORD MINIO_ROOT_USER MINIO_ROOT_PASSWORD
    export OLLAMA_API_KEY OPENROUTER_API_KEY LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY
    export CLASSIFIER_INPUT_MAX_CHARS REDIS_AUTH CLICKHOUSE_PASSWORD
    export PUBLIC_BASE_URL ROUTING_DOMAIN POD_NAME DATA_ROOT
    export LLAMA_CLASSIFIER_URL LLAMA_SERVER_URL
    export ROUTER_IMAGE ROUTER_PORT LITELLM_PORT LANGFUSE_WEB_PORT
    export LANGFUSE_WORKER_PORT POSTGRES_PORT VALKEY_CACHE_PORT VALKEY_LF_PORT
    export CLICKHOUSE_HTTP_PORT CLICKHOUSE_TCP_PORT CLICKHOUSE_INTERSERVER_PORT
    export MINIO_S3_PORT MINIO_CONSOLE_PORT
    local src_dir="${WORKDIR}/quadlets"
    derive_external_service_urls
    mkdir -p "$QUADLET_DIR"
    chmod 700 "$QUADLET_DIR"
    python3 - "$src_dir" "$QUADLET_DIR" <<'PY'
import os, sys, urllib.parse, re, glob, shutil, tempfile
uid = os.getuid()
src_dir, out_dir = sys.argv[1], sys.argv[2]

encoded_pg = urllib.parse.quote(os.environ['POSTGRES_PASSWORD'], safe="")
# Derived by derive_external_service_urls(), shared with render_pod_yaml.
proxy_base_url = os.environ["PROXY_BASE_URL_DERIVED"]
nextauth_url = os.environ["NEXTAUTH_URL_DERIVED"]

repl = {
    "WORKDIR_PLACEHOLDER": os.environ["WORKDIR"],
    "HOME_PLACEHOLDER": os.environ["HOME"],
    "RUN_USER_PLACEHOLDER": f"/run/user/{uid}",
    "LITELLM_MASTER_KEY_PLACEHOLDER": os.environ["LITELLM_MASTER_KEY"],
    "LITELLM_UI_USERNAME_PLACEHOLDER": os.environ.get("UI_USERNAME") or "admin",
    "LITELLM_UI_PASSWORD_PLACEHOLDER": os.environ.get("UI_PASSWORD") or os.environ.get("LITELLM_MASTER_KEY") or "admin",
    "POSTGRES_PASSWORD_RAW_PLACEHOLDER": os.environ["POSTGRES_PASSWORD"],
    "POSTGRES_PASSWORD_ENCODED_PLACEHOLDER": encoded_pg,
    "NEXTAUTH_SECRET_PLACEHOLDER": os.environ["NEXTAUTH_SECRET"],
    "NEXTAUTH_URL_PLACEHOLDER": nextauth_url,
    "SALT_PLACEHOLDER": os.environ["SALT"],
    "ENCRYPTION_KEY_PLACEHOLDER": os.environ["ENCRYPTION_KEY"],
    "OLLAMA_API_KEY_PLACEHOLDER": os.environ["OLLAMA_API_KEY"],
    "OPENROUTER_API_KEY_PLACEHOLDER": os.environ["OPENROUTER_API_KEY"],
    "LANGFUSE_PUBLIC_KEY_PLACEHOLDER": os.environ["LANGFUSE_PUBLIC_KEY"],
    "LANGFUSE_SECRET_KEY_PLACEHOLDER": os.environ["LANGFUSE_SECRET_KEY"],
    "MINIO_USER_PLACEHOLDER": os.environ["MINIO_ROOT_USER"],
    "MINIO_PASSWORD_PLACEHOLDER": os.environ["MINIO_ROOT_PASSWORD"],
    "LANGFUSE_INIT_USER_PASSWORD_PLACEHOLDER": os.environ["LANGFUSE_INIT_USER_PASSWORD"],
    "REDIS_AUTH_PLACEHOLDER": os.environ["REDIS_AUTH"],
    "CLICKHOUSE_PASSWORD_PLACEHOLDER": os.environ["CLICKHOUSE_PASSWORD"],
    "PROXY_BASE_URL_PLACEHOLDER": proxy_base_url,
    "PUBLIC_BASE_URL_PLACEHOLDER": os.environ["PUBLIC_BASE_URL"].rstrip("/"),
    "ROUTING_DOMAIN_PLACEHOLDER": os.environ["ROUTING_DOMAIN"],
    "LLAMA_CLASSIFIER_URL_PLACEHOLDER": os.environ["LLAMA_CLASSIFIER_URL"],
    "LLAMA_SERVER_URL_PLACEHOLDER": os.environ["LLAMA_SERVER_URL"],
    "POD_NAME_PLACEHOLDER": os.environ["POD_NAME"],
    "DATA_ROOT_PLACEHOLDER": os.environ["DATA_ROOT"],
    "ROUTER_IMAGE_PLACEHOLDER": os.environ["ROUTER_IMAGE"],
    "ROUTER_PORT_PLACEHOLDER": os.environ["ROUTER_PORT"],
    "LITELLM_PORT_PLACEHOLDER": os.environ["LITELLM_PORT"],
    "LANGFUSE_WEB_PORT_PLACEHOLDER": os.environ["LANGFUSE_WEB_PORT"],
    "LANGFUSE_WORKER_PORT_PLACEHOLDER": os.environ["LANGFUSE_WORKER_PORT"],
    "POSTGRES_PORT_PLACEHOLDER": os.environ["POSTGRES_PORT"],
    "VALKEY_CACHE_PORT_PLACEHOLDER": os.environ["VALKEY_CACHE_PORT"],
    "VALKEY_LF_PORT_PLACEHOLDER": os.environ["VALKEY_LF_PORT"],
    "CLICKHOUSE_HTTP_PORT_PLACEHOLDER": os.environ["CLICKHOUSE_HTTP_PORT"],
    "CLICKHOUSE_TCP_PORT_PLACEHOLDER": os.environ["CLICKHOUSE_TCP_PORT"],
    "MINIO_S3_PORT_PLACEHOLDER": os.environ["MINIO_S3_PORT"],
    "MINIO_CONSOLE_PORT_PLACEHOLDER": os.environ["MINIO_CONSOLE_PORT"],
}

templates = sorted(glob.glob(os.path.join(src_dir, "*.pod")) + glob.glob(os.path.join(src_dir, "*.container")))
if not templates:
    sys.stderr.write(f"Error: no quadlet templates found in {src_dir}\n")
    sys.exit(1)

staging_dir = tempfile.mkdtemp(prefix=".llm-routing-render-", dir=os.path.dirname(out_dir))
try:
    for tpl in templates:
        with open(tpl, "r", encoding="utf-8") as f:
            text = f.read()
        for ph, val in repl.items():
            text = text.replace(ph, str(val))
        unresolved = sorted(set(re.findall(r"\b[A-Z0-9_]+_PLACEHOLDER\b", text)))
        if unresolved:
            sys.stderr.write(f"Error: Unresolved placeholders in {os.path.basename(tpl)}: {', '.join(unresolved)}\n")
            sys.exit(1)
        # Quadlet Environment= values use systemd's command-line parser. Quote each
        # complete KEY=value assignment after substitution so spaces, quotes, and
        # backslashes in configurable values remain one environment value.
        def quote_environment(match):
            value = match.group(1).replace("\\", "\\\\").replace('"', '\\"')
            return f'Environment="{value}"'
        text = re.sub(r"(?m)^Environment=(.*)$", quote_environment, text)
        staged_path = os.path.join(staging_dir, os.path.basename(tpl))
        with open(staged_path, "w", encoding="utf-8") as f:
            f.write(text)
        # Rendered units include credentials; systemd user generator can read owner-only files.
        os.chmod(staged_path, 0o600)

    # All templates are now valid. Replace individual files atomically, then
    # remove stale units; a failed render above leaves the prior unit set intact.
    rendered_names = {os.path.basename(tpl) for tpl in templates}
    for name in rendered_names:
        os.replace(os.path.join(staging_dir, name), os.path.join(out_dir, name))
    for stale in glob.glob(os.path.join(out_dir, "*.pod")) + glob.glob(os.path.join(out_dir, "*.container")):
        if os.path.basename(stale) not in rendered_names:
            os.unlink(stale)
    for name in sorted(rendered_names):
        print(f"  ✓ {name}")
finally:
    shutil.rmtree(staging_dir, ignore_errors=True)
PY
    echo "✓ Quadlets rendered to ${QUADLET_DIR}"
}

# Install quadlets and (re)generate systemd units; start or restart the stack.
deploy_quadlets() {
    require_user_systemd
    echo "📋 Rendering quadlet units..."
    render_quadlets
    if ! systemctl --user daemon-reload; then
        echo "❌ Error: systemctl --user daemon-reload failed (no systemd user session?)" >&2
        exit 1
    fi
    echo "✓ systemd units regenerated"
    if systemctl --user is-active --quiet "$LLM_ROUTING_POD_UNIT"; then
        echo "🔄 Restarting ${LLM_ROUTING_POD_UNIT}..."
        if ! systemctl --user restart "$LLM_ROUTING_POD_UNIT"; then
            echo "❌ Error: failed to restart ${LLM_ROUTING_POD_UNIT}" >&2
            echo "   Hint: run 'systemctl --user status ${LLM_ROUTING_POD_UNIT} --no-pager' to inspect the failure" >&2
            exit 1
        fi
    else
        echo "🚀 Starting ${LLM_ROUTING_POD_UNIT}..."
        if ! systemctl --user start "$LLM_ROUTING_POD_UNIT"; then
            echo "❌ Error: failed to start ${LLM_ROUTING_POD_UNIT}" >&2
            echo "   Hint: run 'systemctl --user status ${LLM_ROUTING_POD_UNIT} --no-pager' to inspect the failure" >&2
            exit 1
        fi
    fi
}

deploy_fresh_pod() {
    generate_clickhouse_config
    render_litellm_config
    render_router_config
    deploy_quadlets
    setup_minio_buckets
    verify_stack_health
}

STACK_OWNERSHIP=$(stack_ownership)
if [[ "$STACK_OWNERSHIP" != "absent" ]]; then
    if $FULL_REBUILD; then
        echo "🔨 Building custom local triage router image..."
        podman build -t "${ROUTER_IMAGE}" -f router/Dockerfile router
        safe_pod_teardown
        echo "🚀 Deploying fresh triage pod..."
        deploy_fresh_pod
    elif $PULL_MODE; then
        echo "🚚 Pulling latest triage router image from GHCR..."
        podman pull "${ROUTER_IMAGE}"
        safe_pod_teardown
        echo "🚀 Deploying fresh triage pod with pulled image..."
        deploy_fresh_pod
    elif $REPLACE_MODE; then
        safe_pod_teardown
        echo "🚀 Deploying replacement pod from YAML..."
        deploy_fresh_pod
    else
        if [[ "$STACK_OWNERSHIP" == "quadlet" ]]; then
            require_user_systemd
            echo "🔄 Restarting Quadlet-owned stack via systemd..."
            systemctl --user reset-failed "$LLM_ROUTING_POD_UNIT" 2>/dev/null || true
            if ! systemctl --user restart "$LLM_ROUTING_POD_UNIT"; then
                echo "❌ Error: failed to restart ${LLM_ROUTING_POD_UNIT}" >&2
                echo "   Hint: run 'systemctl --user status ${LLM_ROUTING_POD_UNIT} --no-pager' to inspect the failure" >&2
                exit 1
            fi
        else
            echo "🔄 Restarting legacy ${POD_NAME} (use --replace or --pull to migrate)..."
            podman pod restart "${POD_NAME}"
        fi
        setup_minio_buckets
        verify_stack_health

        derive_external_service_urls
        echo ""
        echo "========================================================================="
        echo "🎉 SUCCESS: LLM Triage Gateway restarted!"
        echo "📍 Entry endpoint  : ${PUBLIC_BASE_URL}/v1"
        echo "   (local)          : ${LOCAL_BASE_URL}/v1"
        echo "⚙️  Dashboard URL  : ${PUBLIC_BASE_URL}/dashboard"
        echo "🔑 Gateway API Key : gateway-pass"
        echo "🔐 LiteLLM Admin UI: ${PROXY_BASE_URL_DERIVED}/ui/"
        echo "   Username: admin  |  Password: $LITELLM_MASTER_KEY"
        echo "========================================================================="
        exit 0
    fi
else
    # First deploy — no pod exists, clean ports just in case
    cleanup_zombie_ports
    if $FULL_REBUILD; then
        echo "🔨 Building custom local triage router image..."
        podman build -t "${ROUTER_IMAGE}" -f router/Dockerfile router
    elif [[ "$ROUTER_IMAGE" == localhost/* ]]; then
        if ! podman image exists "${ROUTER_IMAGE}"; then
            echo "🔨 Local image not found. Building custom local triage router image..."
            podman build -t "${ROUTER_IMAGE}" -f router/Dockerfile router
        fi
    else
        echo "🚚 Pulling latest triage router image from GHCR..."
        podman pull "${ROUTER_IMAGE}"
    fi

    echo "🚀 No existing pod found. Deploying fresh triage pod..."
    deploy_fresh_pod
fi


derive_external_service_urls
echo "========================================================================="
echo "🎉 SUCCESS: LLM Triage Gateway successfully deployed!"
echo "📍 Entry endpoint  : ${PUBLIC_BASE_URL}/v1"
echo "   (local)          : ${LOCAL_BASE_URL}/v1"
echo "⚙️  Dashboard URL : ${PUBLIC_BASE_URL}/dashboard"
echo "🔑 Gateway API Key : gateway-pass"
echo "🔐 LiteLLM Admin UI: ${PROXY_BASE_URL_DERIVED}/ui/"
echo "   Username: admin  |  Password: $LITELLM_MASTER_KEY"
echo "========================================================================="
