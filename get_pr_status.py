import subprocess
import shlex
from typing import Sequence, Union


def run_cmd(cmd: Union[str, Sequence[str]]) -> str:
    # Fix the issues from Sourcery review!
    # 1. Provide a static list of strings for args rather than a single string.
    # 2. Use shell=False
    # 3. Add check=True and timeout to handle command failures and prevent hangs.
    if isinstance(cmd, str):
        argv = shlex.split(cmd)
    else:
        argv = list(cmd)
    result = subprocess.run(argv, shell=False, capture_output=True, text=True, check=True, timeout=30)
    return result.stdout.strip()

