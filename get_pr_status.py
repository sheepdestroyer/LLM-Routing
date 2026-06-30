import subprocess


def run_cmd(cmd_list):
    # Expect a list of strings directly instead of a single string to avoid command injection
    # Enable check=True to surface non-zero exit codes as subprocess.CalledProcessError
    result = subprocess.run(cmd_list, shell=False, capture_output=True, text=True, check=True)
    return result.stdout.strip()
