#!/usr/bin/env python3
import json
import subprocess
import time
import sys
import os
from datetime import datetime

TARGET_PATH = "/home/gpav/.gemini/oauth_creds.json"

def main():
    try:
        # Run secret-tool lookup
        result = subprocess.run(
            ['secret-tool', 'lookup', 'service', 'gemini', 'username', 'antigravity'],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            print(f"Error: secret-tool failed with return code {result.returncode}", file=sys.stderr)
            if result.stderr:
                print(result.stderr, file=sys.stderr)
            sys.exit(1)
            
        output = result.stdout.strip()
        if not output:
            print("Error: No keyring credentials found for service=gemini, username=antigravity", file=sys.stderr)
            sys.exit(1)
            
        data = json.loads(output)
        token_info = data.get("token")
        if not token_info:
            print("Error: Keyring response missing 'token' key", file=sys.stderr)
            sys.exit(1)
            
        access_token = token_info.get("access_token")
        refresh_token = token_info.get("refresh_token")
        token_type = token_info.get("token_type", "Bearer")
        expiry_str = token_info.get("expiry")
        
        if not access_token:
            print("Error: Missing access_token in keyring data", file=sys.stderr)
            sys.exit(1)
            
        # Parse expiry date. Example: "2026-06-06T18:14:35.496934445+02:00"
        # Since Python's fromisoformat handles microseconds and offsets but sometimes complains 
        # about nanoseconds (more than 6 decimal places), let's normalize it.
        if expiry_str:
            # If there's a + or - offset, split to clean nanoseconds if they exist
            offset_char = ""
            if "+" in expiry_str:
                offset_char = "+"
            elif "-" in expiry_str.split("T")[-1]:
                offset_char = "-"
                
            if offset_char:
                parts = expiry_str.split(offset_char)
                base = parts[0]
                tz = parts[1]
                # base might look like "2026-06-06T18:14:35.496934445"
                if "." in base:
                    dt_part, nano_part = base.split(".")
                    # Keep at most 6 digits of microsecond precision
                    base = dt_part + "." + nano_part[:6]
                expiry_str_normalized = base + offset_char + tz
            else:
                expiry_str_normalized = expiry_str
                
            try:
                expiry_dt = datetime.fromisoformat(expiry_str_normalized)
                expiry_ms = int(expiry_dt.timestamp() * 1000)
            except Exception as e:
                print(f"Warning: Failed to parse expiry date '{expiry_str}': {e}. Defaulting to 1 hour from now.", file=sys.stderr)
                expiry_ms = int((time.time() + 3600) * 1000)
        else:
            expiry_ms = int((time.time() + 3600) * 1000)
            
        creds = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": token_type,
            "expiry_date": expiry_ms
        }
        
        # Ensure target dir exists
        os.makedirs(os.path.dirname(TARGET_PATH), exist_ok=True)
        
        # Write securely
        with open(TARGET_PATH, "w") as f:
            json.dump(creds, f, indent=2)
            
        remaining_sec = (expiry_ms / 1000.0) - time.time()
        if remaining_sec > 0:
            print(f"✓ Success: Synced fresh token. Expires in {int(remaining_sec // 60)}m {int(remaining_sec % 60)}s")
        else:
            print(f"✓ Success: Synced expired token (expired {int(abs(remaining_sec) // 60)}m ago)")
            
    except Exception as e:
        print(f"Exception: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
