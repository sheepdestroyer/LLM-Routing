import datetime
from unittest.mock import patch

from router import main
from router.main import _save_free_models_roster

def test_save_free_models_roster_success():
    free_models = [{"model": "agent-1"}, {"model": "agent-2"}]

    mock_now = datetime.datetime(2023, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)

    with patch("router.main._atomic_write_json_sync") as mock_atomic_write, \
         patch("datetime.datetime") as m_dt:
        m_dt.now.return_value = mock_now
        m_dt.timezone = datetime.timezone

        _save_free_models_roster(free_models)

        assert mock_atomic_write.call_count == 1
        path, payload = mock_atomic_write.call_args[0]
        assert "free_models_roster.json" in path
        assert payload["models"] == free_models
        assert payload["count"] == 2
        assert payload["updated_at"] == "2023-01-01T12:00:00Z"

def test_save_free_models_roster_exception():
    free_models = []

    with patch("router.main._atomic_write_json_sync", side_effect=PermissionError("Cannot write")):
        # Should not raise exception
        _save_free_models_roster(free_models)
