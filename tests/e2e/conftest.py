import os
import sys
import time
import socket
import tempfile
import subprocess
from contextlib import closing
from pathlib import Path

import pytest
import httpx
from playwright.async_api import async_playwright

root = Path(__file__).resolve().parent.parent.parent


def find_free_port() -> int:
    """Find an available ephemeral TCP port."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


@pytest.fixture
def anyio_backend():
    """Ensure anyio test runner uses the asyncio backend."""
    return "asyncio"


@pytest.fixture(scope="session")
def live_server_url():
    """Start the FastAPI router in an isolated subprocess for end-to-end browser tests."""
    port = find_free_port()
    host = "127.0.0.1"
    base_url = f"http://{host}:{port}"

    env = os.environ.copy()
    env["CONFIG_PATH"] = str(root / "router" / "config.yaml")
    python_paths = [str(root), str(root / "router"), str(root / "scripts")]
    existing_pythonpath = env.get("PYTHONPATH", "")
    if existing_pythonpath:
        python_paths.append(existing_pythonpath)
    env["PYTHONPATH"] = ":".join(python_paths)
    env["LITELLM_MASTER_KEY"] = "sk-litellm-testkey"
    env["ROUTER_API_KEY"] = "sk-router-testkey"
    env["ROUTER_PORT"] = str(port)
    env["LITELLM_READINESS_TIMEOUT"] = "0"

    log_file = tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False)

    # Launch uvicorn in an isolated child process with file redirection to prevent pipe buffer deadlocks
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "router.main:app",
            "--host",
            host,
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(root),
        env=env,
        stdout=log_file,
        stderr=log_file,
    )

    # Wait for server to respond
    start_time = time.time()
    ready = False
    while time.time() - start_time < 15:
        if proc.poll() is not None:
            log_file.seek(0)
            logs = log_file.read()
            log_file.close()
            raise RuntimeError(
                f"Server process terminated early with code {proc.returncode}:\n{logs}"
            )
        try:
            resp = httpx.get(f"{base_url}/favicon.ico", timeout=1.0)
            if resp.status_code in (200, 404):
                ready = True
                break
        except Exception:
            time.sleep(0.2)

    if not ready:
        proc.terminate()
        proc.kill()
        log_file.seek(0)
        logs = log_file.read()
        log_file.close()
        raise RuntimeError(
            f"Live test server failed to respond on {base_url} within 15 seconds:\n{logs}"
        )

    yield base_url

    # Clean shutdown
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    finally:
        log_file.close()
        try:
            os.unlink(log_file.name)
        except OSError:
            pass


@pytest.fixture(scope="session")
def base_url(live_server_url):
    """Base URL pointing to the live test server."""
    return live_server_url


@pytest.fixture
async def page():
    """Async browser page fixture per test using async_playwright."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        context = await browser.new_context()
        page = await context.new_page()
        page.set_default_timeout(10000)
        try:
            yield page
        finally:
            await page.close()
            await context.close()
            await browser.close()
