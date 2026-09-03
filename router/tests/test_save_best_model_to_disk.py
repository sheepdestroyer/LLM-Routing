from unittest.mock import patch
from router import main


def test_save_best_model_to_disk_success():
    best_model = {"model_name": "agent-gemma", "score": 100}

    with patch("router.main._atomic_save_json") as mock_atomic_save:
        main._save_best_model_to_disk(best_model)

        assert mock_atomic_save.call_count == 1
        path, payload = mock_atomic_save.call_args[0]
        assert path.endswith("best_free_model.json")
        assert payload["model_name"] == "agent-gemma"
        assert payload["score"] == 100
        assert "updated_at" in payload


def test_save_best_model_to_disk_exception_handled():
    best_model = {"model_name": "agent-gemma"}

    with patch("router.main._atomic_save_json", side_effect=PermissionError("Cannot write to disk")):
        # Should not raise exception
        main._save_best_model_to_disk(best_model)
