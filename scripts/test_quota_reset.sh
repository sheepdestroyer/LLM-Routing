#!/bin/bash
# Quota reset test script — run after quota resets (~00:56)
set -euo pipefail

echo "=== agy Quota Reset Tests ==="
echo "Time: $(date '+%H:%M:%S')"
echo

TMP_LOG1="$(mktemp /tmp/agy_test_stderr_XXXXXX.log)"
TMP_LOG2="$(mktemp /tmp/agy_test_stderr3_XXXXXX.log)"
trap 'rm -f "$TMP_LOG1" "$TMP_LOG2"' EXIT

# Clean up any stale log entries
echo "1. Testing default Gemini model..."
RC=0
OUTPUT=$(agy --print "Reply with exactly: Gemini OK" 2>"$TMP_LOG1") || RC=$?
if [ "$RC" -eq 0 ] && [ -n "$OUTPUT" ]; then
    echo "   ✅ Gemini: $OUTPUT"
else
    STDERR=$(tail -3 "$TMP_LOG1")
    if echo "$STDERR" | grep -q "RESOURCE_EXHAUSTED\|429\|quota"; then
        echo "   ❌ Gemini: QUOTA EXHAUSTED — still waiting for reset"
        echo "   $STDERR"
        exit 1
    else
        echo "   ❌ Gemini: failed (rc=$RC)"
        echo "   STDERR: $STDERR"
    fi
fi
echo

echo "2. Testing Claude Opus 4.6..."
RC=0
OUTPUT=$(CASCADE_DEFAULT_MODEL_OVERRIDE=claude-opus-3-5@default \
    agy --print "Reply with exactly: Opus OK" 2>"$TMP_LOG2") || RC=$?
if [ "$RC" -eq 0 ] && [ -n "$OUTPUT" ]; then
    echo "   ✅ Opus 4.6: $OUTPUT"
else
    STDERR=$(tail -3 "$TMP_LOG2")
    if echo "$STDERR" | grep -q "RESOURCE_EXHAUSTED\|429\|quota"; then
        echo "   ❌ Opus 4.6: QUOTA EXHAUSTED"
    else
        echo "   ❌ Opus 4.6: failed (rc=$RC)"
        echo "   STDERR: $STDERR"
    fi
fi
echo
echo "=== Tests complete at $(date '+%H:%M:%S') ==="