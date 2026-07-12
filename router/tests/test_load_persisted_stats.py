import json
import pytest
from unittest.mock import patch, mock_open

from router import main
from router.main import load_persisted_stats

@pytest.fixture
def mock_stats():
    # Setup a clean stats dictionary for testing
    clean_stats = {
        "total_requests": 0,
        "nested_dict": {"a": 1, "b": 2},
        "existing_key": "value"
    }
    with patch.dict(main.stats, clean_stats, clear=True):
        yield main.stats

def test_load_persisted_stats_file_not_exists(mock_stats):
    with patch("router.main.os.path.exists", return_value=False) as mock_exists:
        load_persisted_stats()
        mock_exists.assert_called_once_with(main.STATS_JSON_PATH)
        # Stats should remain unchanged
        assert mock_stats["total_requests"] == 0

def test_load_persisted_stats_success(mock_stats):
    mock_data = {
        "total_requests": 100,
        "nested_dict": {"b": 3, "c": 4},
        "new_key": "new_value"
    }
    mock_json = json.dumps(mock_data)

    with patch("router.main.os.path.exists", side_effect=lambda p: p == main.STATS_JSON_PATH):
        with patch("router.main.open", mock_open(read_data=mock_json)):
            with patch("router.main.logger.info") as mock_logger:
                load_persisted_stats()

                # Assert simple value updated via else block
                assert mock_stats["total_requests"] == 100
                # Assert nested_dict updated via if block (b updated, c added, a unchanged)
                assert mock_stats["nested_dict"] == {"a": 1, "b": 3, "c": 4}
                # Assert new_key added via else block
                assert mock_stats["new_key"] == "new_value"
                # Assert existing_key unchanged
                assert mock_stats["existing_key"] == "value"

                mock_logger.assert_called_once_with("✓ Successfully loaded persisted gateway statistics from disk.")

def test_load_persisted_stats_exception(mock_stats):
    with patch("router.main.os.path.exists", return_value=True):
        with patch("router.main.open", side_effect=Exception("Mock read error")):
            with patch("router.main.logger.error") as mock_logger:
                load_persisted_stats()

                # Stats should remain unchanged
                assert mock_stats["total_requests"] == 0

                # Error should be logged
                mock_logger.assert_called_once()
                assert "Failed to load persisted stats: Mock read error" in mock_logger.call_args[0][0]
