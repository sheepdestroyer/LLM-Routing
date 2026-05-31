#!/bin/bash
# Quota reset test script — run after quota resets (~00:56)
set -e

echo "=== agy Quota Reset Tests ==="
echo "Time: $(date '+%H:%M:%S')"
echo

# Clean up any stale log entries
echo "1. Testing default Gemini model..."
OUTPUT=$(agy --print "Reply with exactly: Gemini OK" 2>/tmp/agy_test_stderr.log)
RC=$?
if [ $RC -eq 0 ] && [ -n "$OUTPUT" ]; then
    echo "   ✅ Gemini: $OUTPUT"
else
    STDERR=$(tail -3 /tmp/agy_test_stderr.log)
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
OUTPUT=$(CASCADE_DEFAULT_MODEL_OVERRIDE=claude-opus-4-6@default \
    agy --print "Reply with exactly: Opus OK" 2>/tmp/agy_test_stderr3.log)
RC=$?
if [ $RC -eq 0 ] && [ -n "$OUTPUT" ]; then
    echo "   ✅ Opus 4.6: $OUTPUT"
else
    STDERR=$(tail -3 /tmp/agy_test_stderr3.log)
    if echo "$STDERR" | grep -q "RESOURCE_EXHAUSTED\|429\|quota"; then
        echo "   ❌ Opus 4.6: QUOTA EXHAUSTED"
    else
        echo "   ❌ Opus 4.6: failed (rc=$RC)"
        echo "   STDERR: $STDERR"
    fi
fi
echo
echo "=== Tests complete at $(date '+%H:%M:%S') ==="