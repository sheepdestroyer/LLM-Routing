"""Release-manifest contract; actual image startup is qualified separately."""

import shlex
from pathlib import Path


def test_release_image_contains_required_runtime_modules():
    router = Path(__file__).resolve().parents[1]
    lines = (router / "Dockerfile").read_text().splitlines()
    copied = {token for line in lines if line.startswith("COPY ") for token in shlex.split(line)[1:-1]}
    required = {"main.py", "agy_proxy.py", "circuit_breaker.py", "model_sync.py", "model_metadata.py"}
    assert required <= copied, f"Release image omits mandatory imports: {sorted(required - copied)}"
    assert all((router / name).is_file() for name in required)
