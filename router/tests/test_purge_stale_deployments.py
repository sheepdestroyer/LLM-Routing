import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from router.main import _purge_stale_deployments
import sys

@pytest.mark.asyncio
async def test_purge_stale_deployments_success():
    mock_asyncpg = MagicMock()
    mock_conn = AsyncMock()
    mock_asyncpg.connect = AsyncMock(return_value=mock_conn)

    with patch.dict(sys.modules, {'asyncpg': mock_asyncpg}):
        db_url = "postgres://user:pass@localhost:5432/db"
        pattern = "test-pattern-%"

        # Run function
        await _purge_stale_deployments(db_url, pattern)

        # Assertions
        mock_asyncpg.connect.assert_called_once_with(db_url)
        mock_conn.execute.assert_called_once_with(
            'DELETE FROM "LiteLLM_ProxyModelTable" WHERE model_name LIKE $1',
            pattern
        )
        mock_conn.close.assert_called_once()

@pytest.mark.asyncio
async def test_purge_stale_deployments_execute_exception():
    mock_asyncpg = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.execute.side_effect = Exception("DB Error")
    mock_asyncpg.connect = AsyncMock(return_value=mock_conn)

    with patch.dict(sys.modules, {'asyncpg': mock_asyncpg}):
        db_url = "postgres://user:pass@localhost:5432/db"
        pattern = "test-pattern-%"

        # Run function and assert it raises the exception
        with pytest.raises(Exception, match="DB Error"):
            await _purge_stale_deployments(db_url, pattern)

        # Assertions
        mock_asyncpg.connect.assert_called_once_with(db_url)
        mock_conn.execute.assert_called_once_with(
            'DELETE FROM "LiteLLM_ProxyModelTable" WHERE model_name LIKE $1',
            pattern
        )
        # Ensure close is called even if execute raises an exception
        mock_conn.close.assert_called_once()
