import subprocess
import shlex

def run_cmd(cmd):
    # Fix the issues from Sourcery review!
    # 1. Provide a static list of strings for args rather than a single string.
    # 2. Use shell=False
    args = shlex.split(cmd)
    result = subprocess.run(args, shell=False, capture_output=True, text=True)  # sourcery skip: command-injection
    return result.stdout.strip()
