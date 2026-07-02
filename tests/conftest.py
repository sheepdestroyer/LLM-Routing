import sys
import os
from pathlib import Path

# Dynamic project root discovery
root = Path(__file__).resolve()
while root.parent != root and not (root / ".git").exists():
    root = root.parent

if not (root / ".git").exists():
    raise RuntimeError("Could not find project root: .git directory not found in parent paths.")

# Insert paths to sys.path
for path in [str(root), str(root / "router"), str(root / "scripts")]:
    if path not in sys.path:
        sys.path.insert(0, path)

# Set common environment variables
os.environ.setdefault("CONFIG_PATH", str(root / "router" / "config.yaml"))
