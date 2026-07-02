import os
import sys
from pathlib import Path

# Set CONFIG_PATH for import
os.environ["CONFIG_PATH"] = str(Path(__file__).resolve().parent.parent / "config.yaml")

# Add the parent directory to the path so we can import from router
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
