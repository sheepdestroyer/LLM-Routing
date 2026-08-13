#!/bin/bash
set -euo pipefail

WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_TAR="${1:-llm-routing-deploy.tar.gz}"
if [[ "$OUTPUT_TAR" != /* ]]; then
    OUTPUT_TAR="$(pwd)/$OUTPUT_TAR"
fi

cd "$WORKDIR"
echo "📦 Packaging minimal release deployment bundle -> ${OUTPUT_TAR}..."

# Stamp release version tag into .release_version bundle manifest
RELEASE_VER="${REF_NAME:-${GITHUB_REF_NAME:-$(git describe --tags --abbrev=0 2>/dev/null || echo "latest")}}"
echo "$RELEASE_VER" > .release_version
echo "   Release version stamp: ${RELEASE_VER}"

tar -czf "$OUTPUT_TAR" \
    start-stack.sh \
    quadlets \
    litellm/config.yaml \
    litellm/entrypoint.py \
    router/config.yaml \
    scripts/backup.sh \
    scripts/host_agy_daemon.py \
    scripts/sync_gemini_token.py \
    .release_version \
    README.md

rm -f .release_version

echo "✓ Deployment bundle created: ${OUTPUT_TAR} ($(du -h "$OUTPUT_TAR" | cut -f1))"
