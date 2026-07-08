import pytest
import os
import json
from unittest.mock import patch, mock_open

from router.main import _load_aa_scores
import router.main as main_module

@pytest.fixture
def reset_aa_scores():
    # Store original state
    original_cache = main_module._AA_SCORES_CACHE
    original_loaded = main_module._AA_SCORES_LOADED

    # Reset state for tests
    main_module._AA_SCORES_CACHE = {}
    main_module._AA_SCORES_LOADED = False

    yield

    # Restore original state
    main_module._AA_SCORES_CACHE = original_cache
    main_module._AA_SCORES_LOADED = original_loaded

def test_load_aa_scores_success(reset_aa_scores):
    # Given
    mock_data = json.dumps({"scores": {"model-1": 85.5, "model-2": 42.0}})

    # When
    with patch("builtins.open", mock_open(read_data=mock_data)):
        _load_aa_scores()

    # Then
    assert main_module._AA_SCORES_LOADED is True
    assert main_module._AA_SCORES_CACHE == {"model-1": 85.5, "model-2": 42.0}

def test_load_aa_scores_exception(reset_aa_scores):
    # Given

    # When
    with patch("builtins.open", side_effect=FileNotFoundError("File not found")):
        _load_aa_scores()

    # Then
    assert main_module._AA_SCORES_LOADED is True  # Should be set to True so we don't retry
    assert main_module._AA_SCORES_CACHE == {}

def test_load_aa_scores_already_loaded(reset_aa_scores):
    # Given
    main_module._AA_SCORES_LOADED = True
    main_module._AA_SCORES_CACHE = {"existing": 10.0}

    # When
    with patch("builtins.open") as mock_open_file:
        _load_aa_scores()

    # Then
    mock_open_file.assert_not_called()
    assert main_module._AA_SCORES_CACHE == {"existing": 10.0}
