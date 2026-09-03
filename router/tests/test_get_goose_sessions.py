import pytest
import sys
from unittest.mock import patch, MagicMock

import router.main
from router.main import get_goose_sessions


@pytest.fixture(autouse=True)
def reset_cache():
    router.main._goose_sessions_cache = {"mtime": 0.0, "data": []}
    yield


def test_get_goose_sessions_no_db():
    with patch("os.path.exists", return_value=False):
        assert get_goose_sessions() == []


def test_get_goose_sessions_success():
    mock_sqlite3 = MagicMock()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()

    mock_sqlite3.connect.return_value = mock_conn
    mock_conn.cursor.return_value = mock_cursor

    mock_cursor.fetchall.return_value = [{"id": 1, "name": "s1"}, {"id": 2, "name": "s2"}]

    with patch("os.path.exists", return_value=True):
        with patch("os.path.getmtime", return_value=123.4):
            with patch.dict(sys.modules, {"sqlite3": mock_sqlite3}):
                # First call - should query DB
                result1 = get_goose_sessions()

                assert len(result1) == 2
                assert result1[0] == {"id": 1, "name": "s1"}
                mock_sqlite3.connect.assert_called_once_with("/config/goose_sessions/sessions/sessions.db", timeout=1.0)
                mock_cursor.execute.assert_called_once()
                mock_conn.close.assert_called_once()

                # Reset mocks
                mock_sqlite3.connect.reset_mock()
                mock_cursor.execute.reset_mock()
                mock_conn.close.reset_mock()

                # Second call - should use cache
                result2 = get_goose_sessions()

                assert len(result2) == 2
                assert result2[0] == {"id": 1, "name": "s1"}
                mock_sqlite3.connect.assert_not_called()
                mock_cursor.execute.assert_not_called()
                mock_conn.close.assert_not_called()


def test_get_goose_sessions_cache_invalidation():
    mock_sqlite3 = MagicMock()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()

    mock_sqlite3.connect.return_value = mock_conn
    mock_conn.cursor.return_value = mock_cursor

    mock_cursor.fetchall.side_effect = [[{"id": 1, "name": "s1"}], [{"id": 1, "name": "s1"}, {"id": 2, "name": "s2"}]]

    with patch("os.path.exists", return_value=True):
        with patch("os.path.getmtime", side_effect=[100.0, 200.0]):
            with patch.dict(sys.modules, {"sqlite3": mock_sqlite3}):
                # First call - mtime 100.0
                result1 = get_goose_sessions()
                assert len(result1) == 1
                assert mock_sqlite3.connect.call_count == 1

                # Second call - mtime 200.0, should invalidate cache
                result2 = get_goose_sessions()
                assert len(result2) == 2
                assert mock_sqlite3.connect.call_count == 2


def test_get_goose_sessions_exception():
    mock_sqlite3 = MagicMock()
    mock_sqlite3.connect.side_effect = Exception("DB error")

    with patch("os.path.exists", return_value=True):
        with patch("os.path.getmtime", return_value=123.4):
            with patch.dict(sys.modules, {"sqlite3": mock_sqlite3}):
                result = get_goose_sessions()
                assert result == []
