import subprocess
import json

def run_cmd(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout.strip()

print("Fetching PR reviews and comments...")
# We need to find out what PR 100 is about or what the unaddressed comments are.
# Since we are in the PR branch jules-14409280715745561221-6454f6c5 we need to look for any unresolved issues.
# We'll check git log or any other relevant information.
