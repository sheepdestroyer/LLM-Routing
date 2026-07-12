#!/bin/bash
# upgrade-prod.sh — Sync runtime files from the latest GitHub release and redeploy.
#
# Flow:
#   1. Fetch the latest release tag from GitHub
#   2. Check out a clean copy of that tag to a temp dir
#   3. Rsync runtime files (pod.yaml, start-stack.sh, litellm/, router/, scripts/)
#      into ~/prod/LLM-Routing — data/, backups/, and .env are NEVER touched
#   4. Redeploy with start-stack.sh --pull (pulls latest container images)
#
# Usage:
#   ./upgrade-prod.sh              → latest release
#   ./upgrade-prod.sh v1.2.3       → pin to a specific tag
#   ./upgrade-prod.sh --dry-run    → show what would change, don't apply
set -euo pipefail

REPO="sheepdestroyer/LLM-Routing"
PROD_DIR="${PROD_DIR:-$HOME/prod/LLM-Routing}"
TEMP_DIR=""
DRY_RUN=false
TAG=""

# ── arg parsing ──
for arg in "${@}"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --help|-h)
            echo "Usage: upgrade-prod.sh [--dry-run] [<tag>]"
            echo "  --dry-run  Show what would be synced, exit without changes"
            echo "  <tag>      Pin to a specific release tag (default: latest)"
            exit 0
            ;;
        *) TAG="$arg" ;;
    esac
done

# ── resolve tag ──
if [ -z "$TAG" ]; then
    echo "🔍 Fetching latest release tag from $REPO..."
    TAG=$(curl -sf "https://api.github.com/repos/$REPO/releases/latest" \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])" 2>/dev/null) || {
        echo "❌ Failed to fetch latest release. Pass an explicit tag."
        exit 1
    }
fi
echo "🏷  Target release: $TAG"

# ── clone to temp ──
TEMP_DIR=$(mktemp -d /tmp/llm-routing-upgrade.XXXXXX)
trap 'rm -rf "$TEMP_DIR"' EXIT

echo "📥 Cloning $REPO @ $TAG..."
git clone --depth 1 --branch "$TAG" "https://github.com/$REPO.git" "$TEMP_DIR" 2>&1 | tail -1

# ── verify the tag has the files we need ──
for f in pod.yaml start-stack.sh litellm/ router/ scripts/; do
    if [ ! -e "$TEMP_DIR/$f" ]; then
        echo "❌ Release $TAG is missing expected file/dir: $f"
        exit 1
    fi
done

# ── dry-run: diff summary ──
if $DRY_RUN; then
    echo ""
    echo "── Dry run: files that would change ──"
    diff -rq "$TEMP_DIR/pod.yaml" "$PROD_DIR/pod.yaml" 2>/dev/null || echo "  pod.yaml differs"
    diff -rq "$TEMP_DIR/start-stack.sh" "$PROD_DIR/start-stack.sh" 2>/dev/null || echo "  start-stack.sh differs"
    for dir in litellm router scripts; do
        diff -rq "$TEMP_DIR/$dir" "$PROD_DIR/$dir" 2>/dev/null || echo "  $dir/ differs"
    done
    echo "── End dry run ──"
    exit 0
fi

# ── confirm ──
echo ""
echo "⚠️  This will OVERWRITE the following in $PROD_DIR:"
echo "     pod.yaml  start-stack.sh  litellm/  router/  scripts/"
echo "   .env and data/ are NEVER touched."
echo ""
# Require interactive confirmation in TTY mode; auto-proceed in non-interactive
if [ -t 0 ]; then
    read -rp "Proceed with upgrade to $TAG? [y/N] " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
else
    echo "Non-interactive shell detected, proceeding with upgrade..."
fi

# ── pre-flight: validate PROD_DIR ──
if [ ! -f "$PROD_DIR/.env" ]; then
    echo "❌ $PROD_DIR/.env not found. Is PROD_DIR correct?"
    exit 1
fi

# ── stop the pod gracefully before touching files ──
POD_NAME="${POD_NAME:-agent-router-pod}"
if podman pod exists "$POD_NAME" 2>/dev/null; then
    echo "🛑 Stopping $POD_NAME (SIGTERM, 30s)..."
    podman pod stop -t 30 "$POD_NAME" 2>/dev/null || true
fi

# ── rsync runtime files ──
echo "📋 Syncing runtime files..."
# Sync directories with --delete (clean stale files within each dir)
rsync -a --delete "$TEMP_DIR/litellm/" "$PROD_DIR/litellm/"
rsync -a --delete "$TEMP_DIR/router/" "$PROD_DIR/router/"
rsync -a --delete --exclude="upgrade-prod.sh" "$TEMP_DIR/scripts/" "$PROD_DIR/scripts/"
# Sync files without --delete (no risk to surrounding files)
rsync -a "$TEMP_DIR/pod.yaml" "$PROD_DIR/pod.yaml"
rsync -a "$TEMP_DIR/start-stack.sh" "$PROD_DIR/start-stack.sh"

echo "✓ Runtime files synced from $TAG"

# ── redeploy ──
echo "🚀 Redeploying with --pull (latest container images)..."
cd "$PROD_DIR"
bash start-stack.sh --pull
