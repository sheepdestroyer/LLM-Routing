import datetime
import os
from unittest.mock import patch, mock_open

from router import main
from router.main import _save_free_models_roster

def test_save_free_models_roster_success():
    free_models = [{"model": "agent-1"}, {"model": "agent-2"}]

    mock_now = datetime.datetime(2023, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)

    with patch("builtins.open", mock_open()) as m_open, \
         patch("json.dump") as m_dump, \
         patch("os.replace") as m_replace, \
         patch("datetime.datetime") as m_dt:
        m_dt.now.return_value = mock_now
        m_dt.timezone = datetime.timezone

        _save_free_models_roster(free_models)

        assert m_open.call_count == 1
        opened_path = m_open.call_args[0][0]
        assert "free_models_roster.json.tmp." in opened_path
        assert m_open.call_args[1] == {"encoding": "utf-8"}
        assert m_replace.call_count == 1
        assert m_dump.call_count == 1

        # Check payload
        called_payload = m_dump.call_args[0][0]
        assert called_payload["models"] == free_models
        assert called_payload["count"] == 2
        assert called_payload["updated_at"] == "2023-01-01T12:00:00Z"

        assert m_dump.call_args[1] == {"indent": 2}

def test_save_free_models_roster_exception():
    free_models = []

    with patch("builtins.open", side_effect=PermissionError("Cannot write")):
        # Should not raise exception
        _save_free_models_roster(free_models)
