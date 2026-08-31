from unittest.mock import patch

from router.main import _save_free_models_roster

def test_save_free_models_roster_success():
    free_models = [{"model": "agent-1"}, {"model": "agent-2"}]

    with patch("router.main._atomic_save_json") as mock_atomic_save:
        _save_free_models_roster(free_models)

        assert mock_atomic_save.call_count == 1
        path, payload = mock_atomic_save.call_args[0]
        assert path.endswith("free_models_roster.json")
        assert payload["models"] == free_models
        assert payload["count"] == 2
        assert "updated_at" in payload

def test_save_free_models_roster_exception():
    free_models = []

    with patch("router.main._atomic_save_json", side_effect=PermissionError("Cannot write")):
        # Should not raise exception
        _save_free_models_roster(free_models)
