#!/bin/bash
# upgrade-prod.sh — Sync runtime files from the latest GitHub release and redeploy.
#
# Flow:
#   1. Fetch the latest release tag from GitHub
#   2. Check out a clean copy of that tag to a temp dir
#   3. Rsync runtime files (pod.yaml, start-stack.sh, quadlets/, litellm/, router/, scripts/)
#      into ~/prod/LLM-Routing — data/, backups/, and .env are NEVER touched
#   4. Restart the host units whose files changed (HOST_UNITS: agy-daemon.service runs
#      scripts/host_agy_daemon.py on the host, outside the pod, and keeps the code it started with)
#   5. Redeploy with start-stack.sh --pull (pulls latest container images), then handle the
#      unit files it regenerated (daemon-reload, restart unless it restarted the unit itself)
#
# Usage:
#   ./upgrade-prod.sh              → latest release
#   ./upgrade-prod.sh v1.2.3       → pin to a specific tag
#   ./upgrade-prod.sh --dry-run    → show what would change, don't apply
set -euo pipefail

# ── self-copy guard: re-exec from /tmp so rsync can safely update this script ──
if [[ "${UPGRADE_PROD_SELF_COPIED:-}" != "1" ]]; then
    SELF=$(mktemp /tmp/upgrade-prod-XXXXXX.sh)
    cp "$0" "$SELF"
    chmod +x "$SELF"
    UPGRADE_PROD_SELF_COPIED=1 UPGRADE_PROD_SELF_PATH="$SELF" exec bash "$SELF" "$@"
fi

# ── centralized cleanup ──
cleanup() {
    [[ -n "${TEMP_DIR:-}" ]] && rm -rf "$TEMP_DIR"
    [[ -n "${UPGRADE_PROD_SELF_PATH:-}" ]] && rm -f "$UPGRADE_PROD_SELF_PATH"
}
trap cleanup EXIT

REPO="sheepdestroyer/LLM-Routing"
PROD_DIR="${PROD_DIR:-$HOME/prod/LLM-Routing}"
TEMP_DIR=""
DRY_RUN=false
TAG=""

# ── host units fed from PROD_DIR ──
# systemd user units that run files of PROD_DIR on the host. A running process keeps the code it
# started with, so a unit is restarted when one of its files or its unit file (written by
# start-stack.sh, prod namespace) changed. Entry: "<unit>:<file>[,<file>...]", relative to PROD_DIR.
HOST_UNITS=("agy-daemon.service:scripts/host_agy_daemon.py")
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
# "<sha256> <path>" per tracked file: the content its unit last started with. A digest is replaced
# only once the unit runs the new content (restarted, or stopped and starting with it later), so a
# run that stops or fails before the restart is completed by the next one.
HOST_UNITS_STATE="$PROD_DIR/.host-units.sha256"
declare -A HOST_UNIT_MARK=()
RESTARTED_UNITS=()

# The files a HOST_UNITS entry runs, relative to PROD_DIR, one per line.
host_unit_files() {
    local -a files
    IFS=',' read -r -a files <<< "${1#*:}"
    printf '%s\n' "${files[@]}"
}

# Every path a HOST_UNITS entry tracks: its unit file, then the files it runs.
host_unit_paths() {
    local file
    printf '%s\n' "$SYSTEMD_USER_DIR/${1%%:*}"
    while IFS= read -r file; do
        printf '%s\n' "$PROD_DIR/$file"
    done < <(host_unit_files "$1")
}

# sha256 of a file's content, "absent" when there is no such file.
file_digest() {
    if [ -f "$1" ]; then
        sha256sum < "$1" | cut -d' ' -f1
    else
        echo absent
    fi
}

# The digest recorded for a path in HOST_UNITS_STATE, empty when none.
recorded_digest() {
    local digest path
    [ -f "$HOST_UNITS_STATE" ] || return 0
    while read -r digest path; do
        if [ "$path" = "$1" ]; then
            printf '%s\n' "$digest"
            return 0
        fi
    done < "$HOST_UNITS_STATE"
}

# Record the digest of a path (the state file is replaced atomically).
record_digest() {
    local tmp digest path
    tmp=$(mktemp "$HOST_UNITS_STATE.XXXXXX")
    if [ -f "$HOST_UNITS_STATE" ]; then
        while read -r digest path; do
            if [ "$path" != "$1" ]; then
                printf '%s %s\n' "$digest" "$path"
            fi
        done < "$HOST_UNITS_STATE" > "$tmp"
    fi
    printf '%s %s\n' "$2" "$1" >> "$tmp"
    mv -f "$tmp" "$HOST_UNITS_STATE"
}

# Before the sync: a path without a recorded digest gets the one of its current file. Recorded
# digests are kept: they may be from a run that stopped before restarting the unit.
snapshot_host_units() {
    local entry path
    for entry in "${HOST_UNITS[@]}"; do
        while IFS= read -r path; do
            if [ -z "$(recorded_digest "$path")" ]; then
                record_digest "$path" "$(file_digest "$path")"
            fi
        done < <(host_unit_paths "$entry")
    done
}

# One property of a user unit, empty when the user manager cannot be asked.
unit_property() {
    systemctl --user show "$1" --property="$2" --value --no-pager 2>/dev/null || true
}

# Remember when each unit last became active: a later activation runs the files synced before.
mark_host_units() {
    local entry unit
    for entry in "${HOST_UNITS[@]}"; do
        unit="${entry%%:*}"
        HOST_UNIT_MARK[$unit]=$(unit_property "$unit" ActiveEnterTimestampMonotonic)
    done
}

# True when the unit became active again after mark_host_units (e.g. start-stack.sh restarted it).
restarted_since_mark() {
    local mark="${HOST_UNIT_MARK[$1]:-}"
    [ -n "$mark" ] && [ "$(unit_property "$1" ActiveEnterTimestampMonotonic)" != "$mark" ]
}

# True when a restarted unit is still active with the same main process a few seconds later
# (a unit that crashes on its new files is in auto-restart or failed by then).
unit_settled() {
    local pid
    pid=$(unit_property "$1" MainPID)
    sleep 3
    [ "$(unit_property "$1" ActiveState)" = "active" ] && [ "$(unit_property "$1" MainPID)" = "$pid" ]
}

# Restart each unit whose files differ from the digests it last started with, then record them.
restart_changed_host_units() {
    local entry unit path state status=0
    local -a paths changed
    for entry in "${HOST_UNITS[@]}"; do
        unit="${entry%%:*}"
        mapfile -t paths < <(host_unit_paths "$entry")
        changed=()
        for path in "${paths[@]}"; do
            if [ "$(file_digest "$path")" != "$(recorded_digest "$path")" ]; then
                changed+=("${path#"$PROD_DIR"/}")
            fi
        done
        if [ ${#changed[@]} -eq 0 ]; then
            echo "✓ $unit: unchanged, not restarted"
            continue
        fi
        if [ "$(unit_property "$unit" NeedDaemonReload)" = "yes" ]; then
            echo "🔄 $unit: unit file changed on disk, running systemctl --user daemon-reload"
            if ! systemctl --user daemon-reload --no-pager; then
                echo "❌ systemctl --user daemon-reload failed: $unit not restarted" >&2
                status=1
                continue
            fi
        fi
        state=$(unit_property "$unit" ActiveState)
        case "$state" in
            "")
                echo "❌ Cannot read the state of $unit (no systemd user manager?): not restarted" >&2
                status=1
                continue
                ;;
            active | reloading)
                if restarted_since_mark "$unit"; then
                    echo "✓ $unit already restarted during this upgrade (changed: ${changed[*]})"
                else
                    echo "♻️  Restarting $unit (changed: ${changed[*]})"
                    if ! systemctl --user restart "$unit" --no-pager; then
                        echo "❌ Failed to restart $unit: see systemctl --user status $unit --no-pager" >&2
                        status=1
                        continue
                    fi
                    RESTARTED_UNITS+=("$unit")
                    if unit_settled "$unit"; then
                        echo "✓ Restarted $unit: active, PID $(unit_property "$unit" MainPID)"
                    else
                        # It now runs the new files: restarting again would not help.
                        echo "❌ $unit is not running after the restart: journalctl --user -u $unit -n 50 --no-pager" >&2
                        status=1
                    fi
                fi
                ;;
            *)
                echo "ℹ️  $unit is $state: it runs the new files when it starts (changed: ${changed[*]})"
                ;;
        esac
        for path in "${paths[@]}"; do
            record_digest "$path" "$(file_digest "$path")"
        done
    done
    return "$status"
}

# Dry run: the units the release's files would restart. A unit file that start-stack.sh rewrites is
# only known after the redeploy; a unit file listed here changed since its unit last started.
dry_run_host_units() {
    local entry unit path before release
    local -a changed
    for entry in "${HOST_UNITS[@]}"; do
        unit="${entry%%:*}"
        changed=()
        while IFS= read -r path; do
            before=$(recorded_digest "$path")
            [ -n "$before" ] || before=$(file_digest "$path")
            release="$path"
            case "$path" in
                "$PROD_DIR"/*) release="$TEMP_DIR/${path#"$PROD_DIR"/}" ;;
            esac
            if [ "$(file_digest "$release")" != "$before" ]; then
                changed+=("${path#"$PROD_DIR"/}")
            fi
        done < <(host_unit_paths "$entry")
        if [ ${#changed[@]} -gt 0 ]; then
            echo "  $unit would be restarted (changed: ${changed[*]})"
        else
            echo "  $unit: files unchanged (restarted only if start-stack.sh rewrites its unit file)"
        fi
    done
}

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

echo "📥 Cloning $REPO @ $TAG..."
git clone -q --depth 1 --branch "$TAG" "https://github.com/$REPO.git" "$TEMP_DIR"

# ── verify the tag has the files we need ──
for f in pod.yaml docker-compose.yml start-stack.sh quadlets/ litellm/ router/ scripts/; do
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
    diff -rq "$TEMP_DIR/docker-compose.yml" "$PROD_DIR/docker-compose.yml" 2>/dev/null || echo "  docker-compose.yml differs"
    diff -rq "$TEMP_DIR/start-stack.sh" "$PROD_DIR/start-stack.sh" 2>/dev/null || echo "  start-stack.sh differs"
    for dir in quadlets litellm router scripts; do
        diff -rq "$TEMP_DIR/$dir" "$PROD_DIR/$dir" 2>/dev/null || echo "  $dir/ differs"
    done
    if [ -f "$PROD_DIR/upgrade-prod.sh" ]; then
        diff -q "$TEMP_DIR/scripts/upgrade-prod.sh" "$PROD_DIR/upgrade-prod.sh" >/dev/null 2>&1 \
            || echo "  upgrade-prod.sh (root copy) differs"
    fi
    echo "── Host units ──"
    dry_run_host_units
    echo "── End dry run ──"
    exit 0
fi

# ── confirm ──
echo ""
echo "⚠️  This will OVERWRITE the following in $PROD_DIR:"
echo "     pod.yaml  docker-compose.yml  start-stack.sh  quadlets/  litellm/  router/  scripts/"
echo "     upgrade-prod.sh (root copy, if present)"
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

# ── pre-flight: validate PROD_DIR and required env vars ──
if [ ! -f "$PROD_DIR/.env" ]; then
    echo "❌ $PROD_DIR/.env not found. Is PROD_DIR correct?"
    exit 1
fi

# Source .env to validate required vars are present
# Temporarily disable set -u: .env files may contain unbound variable references
# (e.g., FOO=$BAR where BAR is unset) that would crash with set -u active.
set +u
set -a; source "$PROD_DIR/.env"; set +a
set -u

missing_vars=()
for var in OPENROUTER_API_KEY OLLAMA_API_KEY LLAMA_CLASSIFIER_URL; do
    if [[ ! -v "$var" ]] || [[ -z "${!var}" ]]; then
        missing_vars+=("$var")
    fi
done

if [ ${#missing_vars[@]} -gt 0 ]; then
    echo "❌ Missing required env vars in $PROD_DIR/.env:"
    for var in "${missing_vars[@]}"; do
        echo "   - $var"
    done
    echo ""
    echo "Add them before upgrading. Example:"
    echo "  echo '${missing_vars[0]}=your_value' >> $PROD_DIR/.env"
    exit 1
fi

# ── rsync runtime files ──
# The pod stays running during rsync — it only uses rendered configs from
# DATA_ROOT, not the source files being synced. start-stack.sh --pull
# handles graceful shutdown (including pre-deploy backup) before redeploy.
echo "📋 Syncing runtime files..."
snapshot_host_units
# Sync directories with --delete (clean stale files within each dir)
rsync -a --delete "$TEMP_DIR/quadlets/" "$PROD_DIR/quadlets/"
rsync -a --delete "$TEMP_DIR/litellm/" "$PROD_DIR/litellm/"
rsync -a --delete "$TEMP_DIR/router/" "$PROD_DIR/router/"
rsync -a --delete "$TEMP_DIR/scripts/" "$PROD_DIR/scripts/"
# Sync files without --delete (no risk to surrounding files)
rsync -a "$TEMP_DIR/pod.yaml" "$PROD_DIR/pod.yaml"
rsync -a "$TEMP_DIR/docker-compose.yml" "$PROD_DIR/docker-compose.yml"
rsync -a "$TEMP_DIR/start-stack.sh" "$PROD_DIR/start-stack.sh"
# Runbooks call the root copy (~/prod/LLM-Routing/upgrade-prod.sh): keep it in step with the release.
if [ -f "$PROD_DIR/upgrade-prod.sh" ]; then
    rsync -a "$TEMP_DIR/scripts/upgrade-prod.sh" "$PROD_DIR/upgrade-prod.sh"
fi
echo "$TAG" > "$PROD_DIR/.release_version"

echo "✓ Runtime files synced from $TAG"

# ── host units: restart before the redeploy, so they never run old code next to new files ──
echo "🔁 Host units with changed files..."
units_status=0
mark_host_units
restart_changed_host_units || units_status=1

# ── redeploy ──
echo "🚀 Redeploying with --pull (latest container images)..."
cd "$PROD_DIR"
mark_host_units
deploy_status=0
bash start-stack.sh --pull || deploy_status=$?

# ── host units again: unit files start-stack.sh regenerated (also after a failed redeploy) ──
echo "🔁 Host units after the redeploy..."
restart_changed_host_units || units_status=1
if [ ${#RESTARTED_UNITS[@]} -gt 0 ]; then
    echo "♻️  Host units restarted by this upgrade: ${RESTARTED_UNITS[*]}"
else
    echo "✓ No host unit restarted by this upgrade"
fi

if [ "$deploy_status" -ne 0 ]; then
    echo "❌ start-stack.sh --pull failed (exit $deploy_status)" >&2
    exit "$deploy_status"
fi
if [ "$units_status" -ne 0 ]; then
    echo "❌ Host units need attention (see above); a unit left on its old files is retried by the next run" >&2
    exit "$units_status"
fi
