#!/bin/bash
set -e

# Set working directory
WORKDIR="/home/gpav/Vrac/LAB/AI/LLM-Routing"
cd "$WORKDIR"

# Ensure local volume directories exist on the host for Podman mounts
mkdir -p valkey-data postgres-data langfuse-data

ENV_FILE="${WORKDIR}/.env"

# 1. Load or prompt for OpenRouter API Key
if [ -f "$ENV_FILE" ]; then
    # Load environment variables from secure file
    export $(grep -v '^#' "$ENV_FILE" | xargs)
fi

if [ -z "$OPENROUTER_API_KEY" ]; then
    if [ -t 0 ]; then
        echo "🔑 OpenRouter API Key not found."
        echo -n "Please enter your OpenRouter API Key (input will be hidden): "
        read -rs OPENROUTER_API_KEY
        echo ""
        echo "OPENROUTER_API_KEY=\"$OPENROUTER_API_KEY\"" > "$ENV_FILE"
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

# 2. Extract active refreshed Google OAuth token from Antigravity session
OAUTH_CREDS="/home/gpav/.gemini/oauth_creds.json"
ACTIVE_OAUTH=""

if [ -f "$OAUTH_CREDS" ]; then
    ACTIVE_OAUTH=$(jq -r '.access_token' "$OAUTH_CREDS" 2>/dev/null || echo "")
fi

if [ -z "$ACTIVE_OAUTH" ]; then
    echo "⚠️ Warning: Could not resolve Google OAuth token from $OAUTH_CREDS."
    echo "Gemini models may fail. Please ensure you are logged into Antigravity."
fi

# 3. Use LiteLLM master key from .env if present, otherwise generate a random one
if [ -z "$LITELLM_MASTER_KEY" ]; then
    LITELLM_MASTER_KEY="sk-litellm-$(openssl rand -hex 16)"
    echo "LITELLM_MASTER_KEY=\"$LITELLM_MASTER_KEY\"" >> "$ENV_FILE"
    echo "✓ Generated new LiteLLM master key and saved to $ENV_FILE"
fi

# 4. Generate dynamic deployment definition with secrets injected in memory
cp pod.yaml pod-run.yaml

# Use | as delimiter to handle possible special characters in keys/tokens
sed -i "s|DYNAMIC_LITELLM_MASTER_KEY_PLACEHOLDER|$LITELLM_MASTER_KEY|g" pod-run.yaml
sed -i "s|DYNAMIC_GEMINI_TOKEN_PLACEHOLDER|$ACTIVE_OAUTH|g" pod-run.yaml
sed -i "s|DYNAMIC_OPENROUTER_KEY_PLACEHOLDER|$OPENROUTER_API_KEY|g" pod-run.yaml
sed -i "s|DYNAMIC_LANGFUSE_PUBLIC_KEY_PLACEHOLDER|$LANGFUSE_PUBLIC_KEY|g" pod-run.yaml
sed -i "s|DYNAMIC_LANGFUSE_SECRET_KEY_PLACEHOLDER|$LANGFUSE_SECRET_KEY|g" pod-run.yaml

# Also inject the master key into the router's bind-mounted config
cp router/config.yaml router/config.yaml.tpl
sed -i "s|DYNAMIC_LITELLM_MASTER_KEY_PLACEHOLDER|$LITELLM_MASTER_KEY|g" router/config.yaml

echo "🔨 Building custom local triage router image..."
podman build -t localhost/llm-triage-router:latest -f router/Containerfile router

echo "🛑 Stopping existing agent-router-pod (if running)..."
podman pod stop agent-router-pod 2>/dev/null || true
podman pod rm agent-router-pod 2>/dev/null || true

echo "🚀 Deploying rootless triage pod via Podman..."
podman play kube pod-run.yaml

# 5. Immediately scrub temporary runtime files containing keys from disk
rm -f pod-run.yaml
sleep 3  # Wait for router container to read bind-mounted config before restoring template
mv router/config.yaml.tpl router/config.yaml
echo "🧹 Cleaned up temporary deployment files from disk."

echo "========================================================================"
echo "🎉 SUCCESS: LLM Triage Gateway successfully deployed!"
echo "📍 Entry endpoint  : http://localhost:5000/v1"
echo "⚙️  Dashboard URL : http://localhost:5000/dashboard"
echo "🔑 Gateway API Key : gateway-pass"
echo "🔐 LiteLLM Admin UI: http://localhost:4000/ui"
echo "   Username: admin  |  Password: $LITELLM_MASTER_KEY"
echo "========================================================================"
