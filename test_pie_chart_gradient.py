import pytest
from unittest.mock import patch
from router.main import get_pie_chart_gradient

@pytest.fixture
def mock_stats():
    with patch("router.main.stats") as mock_stats_obj:
        yield mock_stats_obj

def test_get_pie_chart_gradient_empty(mock_stats):
    mock_stats.__getitem__.return_value = {
        "tree": 0,
        "shell": 0,
        "write": 0,
        "view": 0,
        "other": 0
    }
    result = get_pie_chart_gradient()
    assert result == "background: rgba(255, 255, 255, 0.05);"

def test_get_pie_chart_gradient_one_tool(mock_stats):
    mock_stats.__getitem__.return_value = {
        "tree": 100,
        "shell": 0,
        "write": 0,
        "view": 0,
        "other": 0
    }
    result = get_pie_chart_gradient()
    assert result == "background: conic-gradient(#34d399 0.0% 100.0%);"

def test_get_pie_chart_gradient_multiple_tools(mock_stats):
    mock_stats.__getitem__.return_value = {
        "tree": 50,
        "shell": 25,
        "write": 25,
        "view": 0,
        "other": 0
    }
    result = get_pie_chart_gradient()
    assert result == "background: conic-gradient(#34d399 0.0% 50.0%, #fbbf24 50.0% 75.0%, #a78bfa 75.0% 100.0%);"

def test_get_pie_chart_gradient_unrecognized_tool(mock_stats):
    mock_stats.__getitem__.return_value = {
        "unknown_tool": 100
    }
    result = get_pie_chart_gradient()
    assert result == "background: conic-gradient(#94a3b8 0.0% 100.0%);"
