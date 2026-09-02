import pytest
from unittest.mock import AsyncMock, MagicMock, call, patch
import os

from router.main import _register_langfuse_models_in_db, LANGFUSE_MANAGED_MODELS


@pytest.mark.asyncio
async def test_register_langfuse_models_in_db_success():
    mock_conn = AsyncMock()
    mock_asyncpg = MagicMock()
    mock_asyncpg.connect = AsyncMock(return_value=mock_conn)

    with patch.dict("sys.modules", {"asyncpg": mock_asyncpg}), \
         patch.dict(os.environ, {"DATABASE_URL": "postgresql://postgres:pwd@127.0.0.1:5432/postgres"}):
        result = await _register_langfuse_models_in_db(max_retries=3, retry_delay=0.01)
        assert result is True
        mock_asyncpg.connect.assert_called_once_with(
            "postgresql://postgres:pwd@127.0.0.1:5432/langfuse", timeout=5.0
        )
        assert mock_conn.execute.call_count == len(LANGFUSE_MANAGED_MODELS)
        assert mock_conn.close.called

        # Validate each execute call includes exact model ID, name, match pattern, unit, and prices
        for idx, (m_id, m_name, m_pattern, unit, in_p, out_p, tot_p) in enumerate(LANGFUSE_MANAGED_MODELS):
            executed_args = mock_conn.execute.call_args_list[idx][0]
            # SQL statement is arg 0, followed by parameters
            sql, call_id, call_name, call_pattern, call_unit, call_in_p, call_out_p, call_tot_p = executed_args
            assert "INSERT INTO models" in sql
            assert call_id == m_id
            assert call_name == m_name
            assert call_pattern == m_pattern
            assert call_unit == unit
            assert call_in_p == in_p
            assert call_out_p == out_p
            assert call_tot_p == tot_p


@pytest.mark.asyncio
async def test_register_langfuse_models_in_db_retry_success():
    mock_conn = AsyncMock()
    mock_asyncpg = MagicMock()
    # First attempt fails, second attempt succeeds
    mock_asyncpg.connect = AsyncMock(side_effect=[Exception("DB starting up"), mock_conn])

    with patch.dict("sys.modules", {"asyncpg": mock_asyncpg}), \
         patch.dict(os.environ, {"DATABASE_URL": "postgresql://postgres:pwd@127.0.0.1:5432/postgres"}):
        result = await _register_langfuse_models_in_db(max_retries=3, retry_delay=0.01)
        assert result is True
        assert mock_asyncpg.connect.call_count == 2
        assert mock_conn.execute.call_count == len(LANGFUSE_MANAGED_MODELS)
        assert mock_conn.close.called


@pytest.mark.asyncio
async def test_register_langfuse_models_in_db_no_db_url():
    mock_asyncpg = MagicMock()
    mock_asyncpg.connect = AsyncMock()

    with patch.dict("sys.modules", {"asyncpg": mock_asyncpg}), \
         patch.dict(os.environ, {}, clear=True):
        result = await _register_langfuse_models_in_db()
        assert result is False
        mock_asyncpg.connect.assert_not_called()


@pytest.mark.asyncio
async def test_register_langfuse_models_in_db_no_asyncpg():
    with patch.dict("sys.modules", {"asyncpg": None}), \
         patch.dict(os.environ, {"DATABASE_URL": "postgresql://postgres:pwd@127.0.0.1:5432/postgres"}):
        result = await _register_langfuse_models_in_db()
        assert result is False


@pytest.mark.asyncio
async def test_register_langfuse_models_in_db_exhaust_retries():
    mock_asyncpg = MagicMock()
    mock_asyncpg.connect = AsyncMock(side_effect=Exception("DB connection refused"))

    with patch.dict("sys.modules", {"asyncpg": mock_asyncpg}), \
         patch.dict(os.environ, {"DATABASE_URL": "postgresql://postgres:pwd@127.0.0.1:5432/postgres"}):
        result = await _register_langfuse_models_in_db(max_retries=3, retry_delay=0.01)
        assert result is False
        assert mock_asyncpg.connect.call_count == 3
