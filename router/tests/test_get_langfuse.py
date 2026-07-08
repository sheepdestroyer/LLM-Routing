import pytest
from unittest.mock import patch, MagicMock
import os
import sys

from router.main import get_langfuse
import router.main

@pytest.fixture(autouse=True)
def reset_langfuse_client():
    # Reset global _langfuse_client before and after each test
    original = getattr(router.main, "_langfuse_client", None)
    router.main._langfuse_client = None
    yield
    router.main._langfuse_client = original

def test_get_langfuse_success():
    mock_langfuse_module = MagicMock()
    mock_langfuse_instance = MagicMock()
    mock_langfuse_module.Langfuse.return_value = mock_langfuse_instance

    with patch.dict("sys.modules", {"langfuse": mock_langfuse_module}):
        with patch.dict(os.environ, {
            "LANGFUSE_PUBLIC_KEY": "pk",
            "LANGFUSE_SECRET_KEY": "sk",
            "LANGFUSE_HOST": "host"
        }):
            client = get_langfuse()
            assert client is mock_langfuse_instance
            mock_langfuse_module.Langfuse.assert_called_once_with(
                public_key="pk",
                secret_key="sk",
                host="host",
                release="llm-triage-router-v1"
            )

            # Second call should return the cached instance
            mock_langfuse_module.Langfuse.reset_mock()
            client2 = get_langfuse()
            assert client2 is mock_langfuse_instance
            mock_langfuse_module.Langfuse.assert_not_called()

def test_get_langfuse_import_error():
    # Simulate ImportError when importing langfuse
    with patch.dict("sys.modules", {"langfuse": None}): # None means it will fail to import if try
        # Actually it's better to patch builtin __import__
        with patch("builtins.__import__", side_effect=ImportError("mocked import error")):
            client = get_langfuse()
            assert client is None

            # Second call should also return None (sentinel cached)
            client2 = get_langfuse()
            assert client2 is None

@pytest.mark.parametrize("exception_type", [ValueError, TypeError])
def test_get_langfuse_init_errors(exception_type):
    mock_langfuse_module = MagicMock()
    mock_langfuse_module.Langfuse.side_effect = exception_type("mocked error")

    with patch.dict("sys.modules", {"langfuse": mock_langfuse_module}):
        client = get_langfuse()
        assert client is None

        # Second call returns None due to sentinel
        mock_langfuse_module.Langfuse.reset_mock()
        client2 = get_langfuse()
        assert client2 is None
        mock_langfuse_module.Langfuse.assert_not_called()
