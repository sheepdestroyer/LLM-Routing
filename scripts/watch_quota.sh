#!/bin/bash
# Polling loop — checks quota every 30s and runs tests when reset
# Log file to watch
LOG_FILE="$HOME/.gemini/antigravity-cli/cli.log"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_SCRIPT="$SCRIPT_DIR/test_quota_reset.sh"
POLL_INTERVAL=30  # seconds

echo "=== Quota Reset Watcher ==="
echo "Log: $LOG_FILE"
echo "Polling every ${POLL_INTERVAL}s"
echo "Started at: $(date '+%H:%M:%S')"
echo

# Get the initial state
LAST_QUOTA_TIME=$(grep "Resets in" "$LOG_FILE" 2>/dev/null | tail -1 | grep -oP 'Resets in \K[0-9]+m[0-9]+s' || echo "unknown")
echo "Current quota remaining: $LAST_QUOTA_TIME"

while true; do
    # Run agy test with a simple prompt that won't use much quota
    RESULT=$(agy --print "hi" 2>/dev/null)
    if [ -n "$RESULT" ]; then
        echo "✅ QUOTA RESET at $(date '+%H:%M:%S')!"
        echo "   Response: ${RESULT:0:80}"
        echo
        echo "--- Running full test suite ---"
        bash "$TEST_SCRIPT"
        exit 0
    fi
    
    # Check if quota is still mentioned
    NOW=$(grep "Resets in" "$LOG_FILE" 2>/dev/null | tail -1 | grep -oP 'Resets in \K[0-9]+m[0-9]+s' || echo "")
    if [ -n "$NOW" ]; then
        echo "  $(date '+%H:%M:%S') — quota still: $NOW"
    else
        echo "  $(date '+%H:%M:%S') — waiting..."
    fi
    
    sleep $POLL_INTERVAL
done