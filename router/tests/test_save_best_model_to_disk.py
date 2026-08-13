import os
from unittest.mock import patch, mock_open
from router import main

def test_save_best_model_to_disk_success():
    best_model = {"model_name": "agent-gemma", "score": 100}

    mock_file = mock_open()

    with patch("builtins.open", mock_file), \
         patch("json.dump") as mock_json_dump, \
         patch("os.replace") as mock_replace, \
         patch("datetime.datetime") as mock_datetime:

        # Setup mock datetime
        mock_now = mock_datetime.now.return_value
        mock_now.isoformat.return_value = "2023-10-25T12:00:00+00:00"

        main._save_best_model_to_disk(best_model)

        # Verify file opened correctly with atomic temp extension
        assert mock_file.call_count == 1
        opened_path = mock_file.call_args[0][0]
        assert "best_free_model.json.tmp." in opened_path
        assert mock_file.call_args[1] == {"encoding": "utf-8"}
        assert mock_replace.call_count == 1

        # Verify json.dump called with correct payload
        expected_payload = {
            "model_name": "agent-gemma",
            "score": 100,
            "updated_at": "2023-10-25T12:00:00Z"
        }
        mock_json_dump.assert_called_once_with(expected_payload, mock_file(), indent=2)

def test_save_best_model_to_disk_exception_handled():
    best_model = {"model_name": "agent-gemma"}

    mock_file = mock_open()
    mock_file.side_effect = PermissionError("Cannot write to disk")

    # Should not raise exception
    with patch("builtins.open", mock_file):
        main._save_best_model_to_disk(best_model)
