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

# Ensure local volume directories exist on the host for Podman mounts
mkdir -p valkey-data postgres-data langfuse-data clickhouse-data redis-lf-data minio-data

ENV_FILE="${WORKDIR}/.env"

# 1. Load or prompt for OpenRouter API Key
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

if [ -z "$OPENROUTER_API_KEY" ]; then
    if [ -t 0 ]; then
        echo "🔑 OpenRouter API Key not found."
        echo -n "Please enter your OpenRouter API Key (input will be hidden): "
        read -rs OPENROUTER_API_KEY
        echo ""
        echo "OPENROUTER_API_KEY=\"$OPENROUTER_API_KEY\"" > "$ENV_FILE"
        chmod 644 "$ENV_FILE"
        echo "✓ API key saved securely to $ENV_FILE"
    else
        echo "❌ Error: OPENROUTER_API_KEY is not set in your environment or in $ENV_FILE"
        echo "Please run this script interactively first, or create the file manually:"
        echo "  echo 'OPENROUTER_API_KEY=your_key_here' > $ENV_FILE"
        echo "  chmod 600 $ENV_FILE"
        exit 1
    fi
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
    python3 sync_gemini_token.py || echo "⚠️ Warning: Failed to sync Gemini token from keyring"
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

if [ -z "$LITELLM_MASTER_KEY" ]; then
    LITELLM_MASTER_KEY="sk-litellm-$(openssl rand -hex 16)"
    echo "LITELLM_MASTER_KEY=\"$LITELLM_MASTER_KEY\"" >> "$ENV_FILE"
    echo "✓ Generated new LiteLLM master key and saved to $ENV_FILE"
fi

if [ -z "$LITELLM_MASTER_KEY" ]; then
    echo "❌ Error: LITELLM_MASTER_KEY is not set and could not be generated."
    exit 1
fi

# DYNAMIC_LITELLM_MASTER_KEY_PLACEHOLDER in router config is resolved at runtime from env

FULL_REBUILD=false
REPLACE_MODE=false
if [ "${1:-}" = "--full-rebuild" ]; then
    FULL_REBUILD=true
elif [ "${1:-}" = "--replace" ]; then
    REPLACE_MODE=true
fi

# ── Cleanup zombie host-network ports ──
# Podman with host networking can leave stuck LISTEN sockets after SIGKILL.
# This covers ALL ports used by the pod + cross-profile orphans from other
# Hermes profiles (e.g., llm-routing-openrouter) whose container storage
# can leave surviving processes holding ports indefinitely.
cleanup_zombie_ports() {
    local ALL_PORTS="3000 3030 4000 5000 5005 5432 6379 6380 8080 8123 9000 9001 9002 9004 9005 9009"
    
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

    # Wait for MinIO to be ready (console on :9001)
    while [ $waited -lt $MAX_WAIT ]; do
        if curl -sf --max-time 3 http://127.0.0.1:9001 >/dev/null 2>&1; then
            echo "   ✓ MinIO ready after ${waited}s"
            break
        fi
        sleep 3
        waited=$((waited + 3))
    done
    if [ $waited -ge $MAX_WAIT ]; then
        echo "   ⚠️  MinIO not ready after ${MAX_WAIT}s — skipping bucket creation"
        return 1
    fi

    # Ensure mc alias points to the correct MinIO S3 API port (9002, not 9000)
    # The default 'local' alias in the MinIO image points to :9000 which is ClickHouse,
    # not MinIO. We must override it.
    podman exec agent-router-pod-minio-s3 mc alias set local http://127.0.0.1:9002 minioadmin minioadmin 2>/dev/null

    # Create required buckets (idempotent)
    local BUCKETS=("langfuse-events" "proj-triage-gateway-id")
    for bucket in "${BUCKETS[@]}"; do
        if podman exec agent-router-pod-minio-s3 mc ls "local/${bucket}" >/dev/null 2>&1; then
            echo "   ✓ Bucket '${bucket}' exists"
        else
            echo "   + Creating bucket '${bucket}'..."
            podman exec agent-router-pod-minio-s3 mc mb "local/${bucket}" 2>/dev/null || {
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
        if podman exec agent-router-pod-postgres-db pg_isready -U postgres -q 2>/dev/null; then
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
        if curl -sf --max-time 3 http://127.0.0.1:4000/health/readiness >/dev/null 2>&1; then
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
        local resp=$(curl -s --max-time 10 http://127.0.0.1:5000/v1/chat/completions \
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
# ── Safe pod teardown ──
# Graceful stop (SIGTERM with 30s timeout) lets ClickHouse/Postgres flush,
# then force-remove if needed. Avoids data corruption from SIGKILL.
safe_pod_teardown() {
    if podman pod exists agent-router-pod 2>/dev/null; then
        echo "🛑 Gracefully stopping pod (SIGTERM, 30s timeout)..."
        podman pod stop -t 30 agent-router-pod 2>/dev/null || true
        # If still running after graceful attempt, force-remove
        if podman pod exists agent-router-pod 2>/dev/null; then
            echo "⚠️  Graceful stop timed out — force-removing..."
            podman pod rm -f agent-router-pod 2>/dev/null || true
        else
            # Already stopped, just remove
            podman pod rm agent-router-pod 2>/dev/null || true
        fi
        cleanup_zombie_ports
        echo "✓ Pod torn down, ports cleaned"
    fi
}

# Pre-deploy database backup (runs before any pod modification)
# Skip if pod doesn't exist (e.g., after manual cleanup)
if podman pod exists agent-router-pod 2>/dev/null; then
    echo "💾 Taking pre-deploy database backup..."
    bash scripts/backup.sh && echo "✓ Pre-deploy backup saved" || echo "⚠️ Pre-deploy backup skipped"
fi

render_pod_yaml() {
    export WORKDIR HOME LITELLM_MASTER_KEY
    python3 - "$WORKDIR/pod.yaml" <<'PY'
import os, sys
uid = os.getuid()
with open(sys.argv[1], "r", encoding="utf-8") as f:
    text = f.read()
text = text.replace("/home/gpav/Vrac/LAB/AI/LLM-Routing", os.environ["WORKDIR"])
text = text.replace("/home/gpav/", os.environ["HOME"] + "/")
text = text.replace("/run/user/1000", f"/run/user/{uid}")
text = text.replace("sk-lit...33bf", os.environ["LITELLM_MASTER_KEY"])
text = text.replace("postgres:***", "postgres:postgres-local-pw-2026")
sys.stdout.write(text)
PY
}

if podman pod exists agent-router-pod 2>/dev/null; then
    if $FULL_REBUILD; then
        echo "🔨 Building custom local triage router image..."
        podman build -t localhost/llm-triage-router:latest -f router/Dockerfile router
        safe_pod_teardown
        echo "🚀 Deploying fresh triage pod..."
        render_pod_yaml | podman play kube -
        setup_minio_buckets
        verify_stack_health
    elif $REPLACE_MODE; then
        safe_pod_teardown
        echo "🚀 Deploying replacement pod from YAML..."
        render_pod_yaml | podman play kube -
        setup_minio_buckets
        verify_stack_health
    else
        echo "🔄 Restarting existing agent-router-pod (use --replace or --full-rebuild to recreate)..."
        podman pod restart agent-router-pod
        setup_minio_buckets
        verify_stack_health
        echo ""
        echo "========================================================================="
        echo "🎉 SUCCESS: LLM Triage Gateway restarted!"
        echo "📍 Entry endpoint  : http://localhost:5000/v1"
        echo "⚙️  Dashboard URL  : http://localhost:5000/dashboard"
        echo "🔑 Gateway API Key : gateway-pass"
        echo "🔐 LiteLLM Admin UI: http://localhost:4000/ui"
        echo "   Username: admin  |  Password: $LITELLM_MASTER_KEY"
        echo "========================================================================="
        exit 0
    fi
else
    # First deploy — no pod exists, clean ports just in case
    cleanup_zombie_ports
    echo "🔨 Building custom local triage router image..."
    podman build -t localhost/llm-triage-router:latest -f router/Dockerfile router

    echo "🚀 No existing pod found. Deploying fresh triage pod..."
    render_pod_yaml | podman play kube -
    setup_minio_buckets
    verify_stack_health
fi

echo "========================================================================="
echo "🎉 SUCCESS: LLM Triage Gateway successfully deployed!"
echo "📍 Entry endpoint  : http://localhost:5000/v1"
echo "⚙️  Dashboard URL : http://localhost:5000/dashboard"
echo "🔑 Gateway API Key : gateway-pass"
echo "🔐 LiteLLM Admin UI: http://localhost:4000/ui"
echo "   Username: admin  |  Password: $LITELLM_MASTER_KEY"
echo "========================================================================="
