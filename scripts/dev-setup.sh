#!/bin/bash
set -e
# Dev environment setup — creates venv and installs test dependencies
# Run once: ./scripts/dev-setup.sh
# Then activate: source .venv/bin/activate

WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$WORKDIR"

VENV_DIR="${WORKDIR}/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating venv at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
fi

source "${VENV_DIR}/bin/activate"

echo "Installing dependencies..."
pip install --quiet --upgrade pip

# Runtime deps (from router/Dockerfile)
pip install --quiet \
    fastapi "pydantic>=2.0,<3.0" uvicorn httpx pyyaml \
    python-multipart asyncpg langfuse redis aiofiles

# Test deps
pip install --quiet \
    pytest pytest-asyncio anyio

echo ""
echo "Dev venv ready. Activate with:"
echo "  source .venv/bin/activate"
echo ""
echo "Run tests with:"
echo "  python -m pytest router/tests/ -v"
