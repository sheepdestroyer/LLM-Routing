#!/usr/bin/env python3
"""Quick test to understand agy output behavior for quota errors."""
import asyncio
import os

AGY = os.path.expanduser("~/.local/bin/agy")

async def test():
    env = os.environ.copy()
    cmd = [AGY, "--print", "say hi"]

    proc = await asyncio.create_subprocess_exec(
        *cmd, env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=15)
        print(f"returncode: {proc.returncode}")
        print(f"stdout ({len(stdout_bytes)} bytes): {stdout_bytes.decode()[:200]!r}")
        print(f"stderr ({len(stderr_bytes)} bytes): {stderr_bytes.decode()[:200]!r}")
    except asyncio.TimeoutError:
        proc.kill()
        print("TIMEOUT")

    # Also check the log for recent quota lines
    log_path = os.path.expanduser("~/.gemini/antigravity-cli/cli.log")
    if os.path.exists(log_path):
        print(f"\nLast line in cli.log:")
        with open(log_path) as f:
            lines = f.readlines()
            for line in lines[-3:]:
                if "RESOURCE_EXHAUSTED" in line or "quota" in line.lower():
                    print(f"  {line.rstrip()}")

if __name__ == "__main__":
    asyncio.run(test())