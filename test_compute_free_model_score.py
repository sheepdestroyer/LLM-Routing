import pytest
from unittest.mock import patch, mock_open
import json

from router import main as router_main
from router.main import compute_free_model_score

@pytest.fixture(autouse=True)
def reset_cache():
    """Reset the global cache state before each test."""
    router_main._AA_SCORES_CACHE = {}
    router_main._AA_SCORES_LOADED = False
    yield
    router_main._AA_SCORES_CACHE = {}
    router_main._AA_SCORES_LOADED = False

def test_compute_free_model_score_known_model():
    """Test when the model id exists in the cache."""
    mock_data = json.dumps({"scores": {"model-a": 85.5}})
    with patch("builtins.open", mock_open(read_data=mock_data)):
        router_main._load_aa_scores()
        score = compute_free_model_score({"id": "model-a"})
        assert score == 85.5

def test_compute_free_model_score_unknown_model():
    """Test when the model id is not in the cache."""
    mock_data = json.dumps({"scores": {"model-a": 85.5}})
    with patch("builtins.open", mock_open(read_data=mock_data)):
        router_main._load_aa_scores()
        score = compute_free_model_score({"id": "model-b"})
        assert score == 25.0

def test_compute_free_model_score_missing_id():
    """Test when the model dictionary is missing an 'id'."""
    mock_data = json.dumps({"scores": {"model-a": 85.5}})
    with patch("builtins.open", mock_open(read_data=mock_data)):
        router_main._load_aa_scores()
        score = compute_free_model_score({"name": "just a name"})
        assert score == 25.0

def test_compute_free_model_score_file_not_found():
    """Test fallback when the aa_scores.json file is missing or fails to load."""
    with patch("builtins.open", side_effect=FileNotFoundError):
        router_main._load_aa_scores()
        score = compute_free_model_score({"id": "model-a"})
        assert score == 25.0
        assert router_main._AA_SCORES_LOADED is True
        assert router_main._AA_SCORES_CACHE == {}

def test_compute_free_model_score_unloaded():
    """Test that it raises RuntimeError if cache is not loaded."""
    import pytest
    from router.main import compute_free_model_score
    with pytest.raises(RuntimeError, match="AA scores cache must be loaded before calling compute_free_model_score"):
        compute_free_model_score({"id": "model-a"})
