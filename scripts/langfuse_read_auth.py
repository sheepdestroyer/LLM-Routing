"""The Langfuse key pair of LLM-Routing's trace-reading tools.

The stack's own pair (``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY``) only has to write: in prod it
is an Ingestion key, and Langfuse answers its reads with ``403 Access denied. The "traces:read"
permission is required``. The host-side tools that read traces (``scripts/extract_prompts.py``,
``scripts/extract_complex.py``, ``scripts/extract_gapfill.py`` and
``scripts/verification/verify_canonical_endpoints.py``) use a read-scoped pair instead,
``LANGFUSE_READ_PUBLIC_KEY`` / ``LANGFUSE_READ_SECRET_KEY`` in ``.env``, when both are set, and the
main pair otherwise. start-stack.sh never copies the read pair into effective.env, so no container
holds it. Messages name variables, never a value.
"""

from __future__ import annotations

import base64
import sys
from collections.abc import Mapping
from pathlib import Path

READ_PAIR = ("LANGFUSE_READ_PUBLIC_KEY", "LANGFUSE_READ_SECRET_KEY")
MAIN_PAIR = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")


def read_pair(env: Mapping[str, str]) -> tuple[str, str] | None:
    """The pair to read traces with: LANGFUSE_READ_* when both are set, else LANGFUSE_*; None when
    neither pair is complete. A half-set read pair is reported on stderr and ignored."""
    read_public, read_secret = (env.get(name) or "" for name in READ_PAIR)
    if read_public and read_secret:
        return read_public, read_secret
    if read_public or read_secret:
        missing = READ_PAIR[0] if read_secret else READ_PAIR[1]
        print(f"⚠ {missing} is not set: reading traces with {MAIN_PAIR[0]}/{MAIN_PAIR[1]}", file=sys.stderr)
    public, secret = (env.get(name) or "" for name in MAIN_PAIR)
    if public and secret:
        return public, secret
    return None


def load_env_file(path: Path) -> dict[str, str]:
    """The NAME=value lines of a .env file, surrounding quotes removed (comments and blank lines skipped)."""
    env: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def trace_reader_auth(env_path: Path) -> str:
    """The base64 credentials of an ``Authorization: Basic`` header for reading traces."""
    pair = read_pair(load_env_file(env_path))
    if pair is None:
        raise SystemExit(
            f"Set {READ_PAIR[0]}/{READ_PAIR[1]} (a key that may read traces) or "
            f"{MAIN_PAIR[0]}/{MAIN_PAIR[1]} in {env_path}"
        )
    return base64.b64encode(f"{pair[0]}:{pair[1]}".encode()).decode()
