import os
import sqlite3
import tempfile
import pytest
from unittest import mock

from router.main import get_goose_sessions

@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(delete=False) as f:
        db_path = f.name

    real_conn = sqlite3.connect(db_path)
    cursor = real_conn.cursor()
    cursor.execute('''
        CREATE TABLE sessions (
            id TEXT, name TEXT, description TEXT, created_at TEXT,
            updated_at TEXT, accumulated_total_tokens INTEGER, goose_mode TEXT
        )
    ''')
    cursor.execute('''
        INSERT INTO sessions
        VALUES ('1', 'Session 1', 'Desc 1', '2023-01-01', '2023-01-02', 100, 'auto'),
               ('2', 'Session 2', 'Desc 2', '2023-01-03', '2023-01-04', 200, 'manual')
    ''')
    real_conn.commit()
    real_conn.close()

    yield db_path

    os.remove(db_path)

def test_get_goose_sessions_db_not_found():
    """Test when the database file does not exist."""
    with mock.patch("os.path.exists", return_value=False):
        result = get_goose_sessions()
        assert result == []

def test_get_goose_sessions_success(temp_db):
    """Test successfully querying the database."""
    with mock.patch("os.path.exists", return_value=True):
        real_connect = sqlite3.connect

        def fake_connect(*args, **kwargs):
            return real_connect(temp_db)

        with mock.patch("sqlite3.connect", side_effect=fake_connect):
            result = get_goose_sessions()

            assert len(result) == 2
            # Notice the query in `get_goose_sessions` has `ORDER BY updated_at DESC`,
            # so Session 2 should come before Session 1
            assert result[0]["id"] == "2"
            assert result[0]["name"] == "Session 2"
            assert result[0]["accumulated_total_tokens"] == 200

            assert result[1]["id"] == "1"
            assert result[1]["name"] == "Session 1"
            assert result[1]["goose_mode"] == "auto"

def test_get_goose_sessions_db_error():
    """Test handling of a database error gracefully."""
    real_exists = os.path.exists

    def fake_exists(path):
        if path == "/config/goose_sessions/sessions/sessions.db":
            return True
        return real_exists(path)

    with mock.patch("os.path.exists", side_effect=fake_exists):
        with mock.patch("sqlite3.connect", side_effect=sqlite3.OperationalError("Database is locked")):
            result = get_goose_sessions()
            assert result == []
