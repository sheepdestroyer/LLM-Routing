from unittest.mock import patch
from pathlib import Path
import pytest
from fastapi.testclient import TestClient


def test_favicon_ico_endpoint(monkeypatch):
    # Set standard environment variables before importing app
    monkeypatch.setenv("LITELLM_MASTER_KEY", "test-key")
    monkeypatch.setenv("ROUTER_API_KEY", "test-key")

    from router.main import app

    client = TestClient(app)
    response = client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"] in ("image/x-icon", "image/vnd.microsoft.icon")


def test_favicon_ico_not_found(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "test-key")
    monkeypatch.setenv("ROUTER_API_KEY", "test-key")

    from router.main import app

    with patch("router.main.STATIC_DIR", Path("/nonexistent/static/dir")):
        client = TestClient(app)
        response = client.get("/favicon.ico")
        assert response.status_code == 404
        assert response.json()["detail"] == "Favicon not found"
