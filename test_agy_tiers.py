#!/usr/bin/env python3
"""
Test script for agy proxy fallback tiers.
Tests all 3 model tiers and verifies session continuation.
"""
import asyncio
import json
import os
import sys
import time

AGY = os.path.expanduser("~/.local/bin/agy")
CACHE_FILE = os.path.expanduser("~/.gemini/antigravity-cli/cache/last_conversations.json")

TIERS = [
    {"name": "Gemini 3.5 Flash",    "override": ""},
    {"name": "Claude Opus 4.6",     "override": "claude-opus-4-6@default"},
]

async def run_tier_test(tier, prompt="say hello in one word", conversation_id=None):
    """Test a single agy tier and return (success, output, conv_id)."""
    env = os.environ.copy()
    if tier["override"]:
        env["CASCADE_DEFAULT_MODEL_OVERRIDE"] = tier["override"]

    cmd = [AGY]
    if conversation_id:
        cmd.extend(["--conversation", conversation_id])
    cmd.extend(["--print", prompt])

    print(f"\n  🧪 Testing {tier['name']}... ", end="", flush=True)
    if conversation_id:
        print(f"(continuing {conversation_id[:8]}...) ", end="", flush=True)

    start = time.time()
    proc = await asyncio.create_subprocess_exec(
        *cmd, env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=30)
        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        elapsed = time.time() - start

        # Get conversation ID from cache
        conv_id = None
        try:
            if os.path.exists(CACHE_FILE):
                with open(CACHE_FILE) as f:
                    data = json.load(f)
                conv_id = data.get(os.getcwd())
        except:
            pass

        # Check for quota exhaustion
        if "RESOURCE_EXHAUSTED" in stderr or "code 429" in stderr:
            print(f"❌ QUOTA EXHAUSTED ({elapsed:.1f}s)")
            print(f"     stderr: {stderr[:100]}")
            return False, None, conv_id

        if stdout:
            print(f"✅ OK ({elapsed:.1f}s, {len(stdout)} chars)")
            print(f"     response: {stdout[:80]}...")
            return True, stdout, conv_id
        else:
            print(f"⚠️  EMPTY RESPONSE ({elapsed:.1f}s)")
            print(f"     stderr: {stderr[:200]}")
            return False, None, conv_id

    except asyncio.TimeoutError:
        proc.kill()
        print(f"❌ TIMEOUT (30s)")
        return False, None, None

async def main():
    print("=" * 60)
    print("  agy Proxy Tier Test Suite")
    print("=" * 60)

    # Test 1: Each tier independently (new conversations)
    print("\n--- Test 1: Independent Tier Tests ---")
    conv_ids = {}
    for tier in TIERS:
        success, output, conv_id = await run_tier_test(tier)
        conv_ids[tier["name"]] = conv_id
        if not success:
            print(f"  ⚠️  Tier {tier['name']} failed — subsequent tests may use different model")

    # Test 2: Session continuation (same conversation across tiers)
    print("\n\n--- Test 2: Session Continuation ---")
    # Get the last successful conversation ID
    successful_conv = None
    for tier in TIERS:
        if tier["name"] in conv_ids and conv_ids[tier["name"]]:
            successful_conv = conv_ids[tier["name"]]
            break

    if successful_conv:
        print(f"  Continuing conversation {successful_conv[:8]}...")
        for tier in TIERS:
            success, output, _ = await run_tier_test(
                tier,
                prompt="continue our conversation, say one more word",
                conversation_id=successful_conv
            )
            if success:
                break
    else:
        print("  ⚠️  No successful conversation to continue")

    # Test 3: Auto-fallback chain (simulate proxy behavior)
    print("\n\n--- Test 3: Proxy Fallback Chain ---")
    proxy_prompt = "what's 2+2? answer in one word"
    for tier in TIERS:
        success, output, conv_id = await run_tier_test(tier, prompt=proxy_prompt)
        if success:
            print(f"\n  ✅ Proxy would use: {tier['name']}")
            break

    print("\n" + "=" * 60)
    print("  Tests complete!")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())