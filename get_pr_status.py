import subprocess

def run_cmd(cmd_list):
    # Expect a list of strings directly instead of a single string to avoid command injection
    result = subprocess.run(cmd_list, shell=False, capture_output=True, text=True)
    return result.stdout.strip()
