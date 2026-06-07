#!/bin/bash
set -e

# Usage:
#   ./start-stack.sh              → Restart existing pod (fast, preserves logs)
#   ./start-stack.sh --replace    → Graceful stop + clean ports + redeploy pod
#                                    (for pod.yaml changes: ports, probes, env vars)
#   ./start-stack.sh --full-rebuild → Same as --replace + rebuild router image
#                                      (for router/Containerfile changes)

# Set working directory
WORKDIR="/home/gpav/Vrac/LAB/AI/LLM-Routing"
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
OAUTH_CREDS="/home/gpav/.gemini/oauth_creds.json"
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

# 3. Use LiteLLM master key from .env if present, otherwise generate a random one
if [ -z "$LITELLM_MASTER_KEY" ]; then
    LITELLM_MASTER_KEY="sk-litellm-$(openssl rand -hex 16)"
    echo "LITELLM_MASTER_KEY=\"$LITELLM_MASTER_KEY\"" >> "$ENV_FILE"
    echo "✓ Generated new LiteLLM master key and saved to $ENV_FILE"
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
# ClickHouse and Minio are the primary victims — their ports survive container
# death and block the replacement from binding.
cleanup_zombie_ports() {
    local PORTS="8123 9000 9001 9002 9004 9005 9009"
    # Attempt fuser kill first (works if process still alive)
    for port in $PORTS; do
        fuser -k "${port}/tcp" 2>/dev/null || true
    done
    # Wait up to 60s for kernel to release orphaned sockets
    local waited=0
    while [ $waited -lt 60 ]; do
        local stuck=$(ss -tlnpH 2>/dev/null | grep -cE ":(8123|900[0-9])") || true
        if [ "${stuck:-0}" -eq 0 ]; then
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
    local final=$(ss -tlnpH 2>/dev/null | grep -cE ":(8123|900[0-9])") || true
    echo "⚠️  Warning: ${final:-0} zombie port(s) survived 60s cleanup wait"
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
echo "💾 Taking pre-deploy database backup..."
bash scripts/backup.sh && echo "✓ Pre-deploy backup saved" || echo "⚠️ Pre-deploy backup skipped"

if podman pod exists agent-router-pod 2>/dev/null; then
    if $FULL_REBUILD; then
        echo "🔨 Building custom local triage router image..."
        podman build -t localhost/llm-triage-router:latest -f router/Containerfile router
        safe_pod_teardown
        echo "🚀 Deploying fresh triage pod..."
        podman play kube "$WORKDIR/pod.yaml"
    elif $REPLACE_MODE; then
        safe_pod_teardown
        echo "🚀 Deploying replacement pod from YAML..."
        podman play kube "$WORKDIR/pod.yaml"
    else
        echo "🔄 Restarting existing agent-router-pod (use --replace or --full-rebuild to recreate)..."
        podman pod restart agent-router-pod
        echo "✅ Pod restarted. Container IDs preserved — logs survive in podman logs."
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
    podman build -t localhost/llm-triage-router:latest -f router/Containerfile router

    echo "🚀 No existing pod found. Deploying fresh triage pod..."
    podman play kube "$WORKDIR/pod.yaml"
fi

echo "========================================================================="
echo "🎉 SUCCESS: LLM Triage Gateway successfully deployed!"
echo "📍 Entry endpoint  : http://localhost:5000/v1"
echo "⚙️  Dashboard URL : http://localhost:5000/dashboard"
echo "🔑 Gateway API Key : gateway-pass"
echo "🔐 LiteLLM Admin UI: http://localhost:4000/ui"
echo "   Username: admin  |  Password: $LITELLM_MASTER_KEY"
echo "========================================================================="
