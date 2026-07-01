#!/usr/bin/env python3
import subprocess
import json
import sys
from typing import Sequence


def run_cmd(argv: Sequence[str]) -> str:
    """Runs a command and returns stripped stdout."""
    result = subprocess.run(argv, shell=False, capture_output=True, text=True, check=True, timeout=30)
    return result.stdout.strip()


def get_pr_status(pr_id: str = "") -> None:
    """Fetches and prints the status of a PR using gh CLI."""
    cmd = ["gh", "pr", "view"]
    if pr_id:
        cmd.append(pr_id)
    cmd.extend(["--json", "state,reviewDecision,statusCheckRollup"])

    try:
        output = run_cmd(cmd)
        data = json.loads(output)

        state = data.get("state")
        review = data.get("reviewDecision") or "NONE"
        checks = data.get("statusCheckRollup", [])

        # Summarize checks
        success_count = 0
        total_count = len(checks)
        for check in checks:
            # gh CLI returns conclusion for CheckRun and state for StatusContext
            conclusion = check.get("conclusion") or check.get("state")
            if conclusion == "SUCCESS":
                success_count += 1

        print(f"PR Status: {state}")
        print(f"Review Decision: {review}")
        print(f"Checks: {success_count}/{total_count} passed")

    except subprocess.CalledProcessError as e:
        print(f"Error: Failed to fetch PR status: {e.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError:
        print("Error: Failed to parse gh CLI output", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    pr_id = sys.argv[1] if len(sys.argv) > 1 else ""
    get_pr_status(pr_id)


if __name__ == "__main__":
    main()
