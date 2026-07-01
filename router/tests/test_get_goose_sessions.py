import pytest
import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

os.environ["CONFIG_PATH"] = str(Path(__file__).resolve().parent.parent / "config.yaml")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import get_goose_sessions

def test_get_goose_sessions_no_db():
    with patch('os.path.exists', return_value=False):
        assert get_goose_sessions() == []

def test_get_goose_sessions_success():
    mock_sqlite3 = MagicMock()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()

    mock_sqlite3.connect.return_value = mock_conn
    mock_conn.cursor.return_value = mock_cursor

    mock_cursor.fetchall.return_value = [
        {"id": 1, "name": "s1"},
        {"id": 2, "name": "s2"}
    ]

    with patch('os.path.exists', return_value=True):
        with patch.dict(sys.modules, {'sqlite3': mock_sqlite3}):
            result = get_goose_sessions()

            assert len(result) == 2
            assert result[0] == {"id": 1, "name": "s1"}
            mock_sqlite3.connect.assert_called_once_with("/config/goose_sessions/sessions/sessions.db", timeout=1.0)
            mock_cursor.execute.assert_called_once()
            mock_conn.close.assert_called_once()

def test_get_goose_sessions_exception():
    mock_sqlite3 = MagicMock()
    mock_sqlite3.connect.side_effect = Exception("DB error")

    with patch('os.path.exists', return_value=True):
        with patch.dict(sys.modules, {'sqlite3': mock_sqlite3}):
            result = get_goose_sessions()
            assert result == []
