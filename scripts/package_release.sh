#!/bin/bash
set -e

WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_TAR="${1:-llm-routing-deploy.tar.gz}"

cd "$WORKDIR"
echo "📦 Packaging minimal release deployment bundle -> ${OUTPUT_TAR}..."

tar -czf "$OUTPUT_TAR" \
    start-stack.sh \
    quadlets \
    litellm/config.yaml \
    litellm/entrypoint.py \
    router/config.yaml \
    scripts/backup.sh \
    scripts/host_agy_daemon.py \
    scripts/sync_gemini_token.py \
    README.md

echo "✓ Deployment bundle created: ${OUTPUT_TAR} ($(du -h "$OUTPUT_TAR" | cut -f1))"
