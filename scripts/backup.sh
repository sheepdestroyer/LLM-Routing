#!/bin/bash
set -e

# ============================================================
# LLM-Routing Stack Backup Script
# Run manually: ./scripts/backup.sh
# Scheduled via: systemctl --user enable llm-backup.timer
# ============================================================

WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="${WORKDIR}/backups"
RETENTION_DAYS=14
LOG_FILE="/tmp/llm-backup-${TIMESTAMP}.log"

# Source .env for POD_NAME (with prod default)
if [ -f "${WORKDIR}/.env" ]; then
    set -a; source "${WORKDIR}/.env"; set +a
fi
POD_NAME="${POD_NAME:-agent-router-pod}"

mkdir -p "$BACKUP_DIR"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

# ---- Wait for PostgreSQL to be ready (up to 30s) ----
log "⏳ Checking PostgreSQL readiness..."
PG_READY=0
# Check if container exists AND is running before looping
PG_RUNNING=$(podman inspect --format '{{.State.Running}}' ${POD_NAME}-postgres-db 2>/dev/null || echo "false")
if [ "$PG_RUNNING" != "true" ]; then
    log "⚠️  PostgreSQL container not running — skipping DB backup"
else
    for i in {1..15}; do
        if podman exec ${POD_NAME}-postgres-db pg_isready -U postgres 2>/dev/null; then
            PG_READY=1
            log "✅ PostgreSQL is ready"
            break
        fi
        log "⏳ PostgreSQL not ready, retrying in 2s ($i/15)..."
        sleep 2
    done
fi

# ---- PostgreSQL Databases ----
if [ $PG_READY -eq 1 ]; then
    for db in postgres langfuse; do
        FILE="${BACKUP_DIR}/${db}_db_${TIMESTAMP}.dump"
        if podman exec ${POD_NAME}-postgres-db \
            pg_dump -U postgres -d "$db" -F c > "$FILE"; then
            log "✅ ${db} db: $(ls -lh "$FILE" | awk '{print $5}')"
        else
            log "❌ ${db} db: FAILED"
        fi
    done
else
    log "❌ PostgreSQL db backup: FAILED (PostgreSQL was not ready after 30s)"
fi

# ---- Config Files (lightweight copy) ----
CONFIG_SNAPSHOT="${BACKUP_DIR}/configs_${TIMESTAMP}.tar.gz"
tar czf "$CONFIG_SNAPSHOT" \
    -C "$WORKDIR" \
    litellm/config.yaml \
    router/config.yaml \
    router/main.py \
    router/memory_mcp.py \
    router/agentic_scores.json \
    pod.yaml .env
log "✅ configs: $(ls -lh "$CONFIG_SNAPSHOT" | awk '{print $5}')"

# ---- Prune old backups ----
old=$(find "$BACKUP_DIR" -name "*.dump" -o -name "*.tar.gz" -mtime +$RETENTION_DAYS | wc -l)
find "$BACKUP_DIR" -name "*.dump" -o -name "*.tar.gz" -mtime +$RETENTION_DAYS -delete
log "🧹 Pruned $old backup(s) older than ${RETENTION_DAYS} days"

log "✅ Backup complete → ${BACKUP_DIR}/${TIMESTAMP}"