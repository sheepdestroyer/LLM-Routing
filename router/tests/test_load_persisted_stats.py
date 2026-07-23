import json
from unittest.mock import mock_open, patch

import pytest
import router.main as main
from router.main import load_persisted_stats


@pytest.fixture
def mock_stats():
    # Save original stats
    orig_stats = main.stats.copy()
    # Reset stats for testing
    main.stats.clear()
    main.stats.update(
        {
            "total_requests": 0,
            "nested_dict": {"a": 1, "b": 2},
            "existing_key": "value",
        }
    )
    yield main.stats
    # Restore original stats
    main.stats.clear()
    main.stats.update(orig_stats)


def test_load_persisted_stats_file_not_exists(mock_stats):
    with patch("router.main.os.path.exists", return_value=False) as mock_exists:
        load_persisted_stats()
        mock_exists.assert_called_once_with(main.STATS_JSON_PATH)
        assert mock_stats["total_requests"] == 0


def test_load_persisted_stats_success(mock_stats):
    mock_data = {
        "total_requests": 100,
        "nested_dict": {"b": 3, "c": 4},
        "new_key": "new_value",
    }
    mock_json = json.dumps(mock_data)

    def mock_exists_side_effect(p):
        return p == main.STATS_JSON_PATH

    with patch("router.main.os.path.exists", side_effect=mock_exists_side_effect):
        with patch("router.main.open", mock_open(read_data=mock_json)):
            with patch("router.main.logger.info") as mock_logger:
                load_persisted_stats()

                assert mock_stats["total_requests"] == 100
                assert mock_stats["nested_dict"] == {"a": 1, "b": 3, "c": 4}
                assert mock_stats["new_key"] == "new_value"
                assert mock_stats["existing_key"] == "value"
                mock_logger.assert_called_once_with("✓ Successfully loaded persisted gateway statistics from disk.")


def test_load_persisted_stats_timeline_success(mock_stats):
    mock_data = {"total_requests": 100}
    mock_timeline_data = [{"time": 1, "val": "A"}]

    def mock_open_side_effect(file, *args, **kwargs):
        if file == main.STATS_JSON_PATH:
            return mock_open(read_data=json.dumps(mock_data))()
        else:
            return mock_open(read_data=json.dumps(mock_timeline_data))()

    with patch("router.main.os.path.exists", return_value=True):
        with patch("router.main.open", side_effect=mock_open_side_effect):
            load_persisted_stats()

            assert mock_stats["total_requests"] == 100
            assert mock_stats["timeline"] == mock_timeline_data


def test_load_persisted_stats_timeline_exception(mock_stats):
    mock_data = {"total_requests": 100}

    def mock_open_side_effect(file, *args, **kwargs):
        if file == main.STATS_JSON_PATH:
            return mock_open(read_data=json.dumps(mock_data))()
        else:
            raise Exception("Mock timeline read error")

    with patch("router.main.os.path.exists", return_value=True):
        with patch("router.main.open", side_effect=mock_open_side_effect):
            load_persisted_stats()

            assert mock_stats["total_requests"] == 100
            assert "timeline" not in mock_stats


def test_load_persisted_stats_exception(mock_stats):
    with patch("router.main.os.path.exists", return_value=True):
        with patch("router.main.open", side_effect=Exception("Mock read error")):
            with patch("router.main.logger.error") as mock_logger:
                load_persisted_stats()

                assert mock_stats
