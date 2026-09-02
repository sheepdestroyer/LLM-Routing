import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import os

from router.main import _register_langfuse_models_in_db


@pytest.mark.asyncio
async def test_register_langfuse_models_in_db_success():
    mock_conn = AsyncMock()
    mock_asyncpg = MagicMock()
    mock_asyncpg.connect = AsyncMock(return_value=mock_conn)

    with patch.dict("sys.modules", {"asyncpg": mock_asyncpg}), \
         patch.dict(os.environ, {"DATABASE_URL": "postgresql://postgres:pwd@127.0.0.1:5432/postgres"}):
        await _register_langfuse_models_in_db()
        mock_asyncpg.connect.assert_called_once_with(
            "postgresql://postgres:pwd@127.0.0.1:5432/langfuse", timeout=5.0
        )
        assert mock_conn.execute.call_count >= 7
        assert mock_conn.close.called


@pytest.mark.asyncio
async def test_register_langfuse_models_in_db_no_db_url():
    mock_asyncpg = MagicMock()
    mock_asyncpg.connect = AsyncMock()

    with patch.dict("sys.modules", {"asyncpg": mock_asyncpg}), \
         patch.dict(os.environ, {}, clear=True):
        await _register_langfuse_models_in_db()
        mock_asyncpg.connect.assert_not_called()


@pytest.mark.asyncio
async def test_register_langfuse_models_in_db_exception_non_fatal():
    mock_asyncpg = MagicMock()
    mock_asyncpg.connect = AsyncMock(side_effect=Exception("DB connection error"))

    with patch.dict("sys.modules", {"asyncpg": mock_asyncpg}), \
         patch.dict(os.environ, {"DATABASE_URL": "postgresql://postgres:pwd@127.0.0.1:5432/postgres"}):
        # Should catch exception and not raise
        await _register_langfuse_models_in_db()
