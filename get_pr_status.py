import subprocess
from typing import Sequence


def run_cmd(argv: Sequence[str]) -> str:
    # Fix the issues from Sourcery review!
    # 1. Provide a static list of strings for args rather than a single string.
    # 2. Use shell=False
    # 3. Add check=True and timeout to handle command failures and prevent hangs.
    result = subprocess.run(argv, shell=False, capture_output=True, text=True, check=True, timeout=30)
    return result.stdout.strip()

