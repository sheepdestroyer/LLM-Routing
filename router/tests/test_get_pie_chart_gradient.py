import pytest
from unittest.mock import patch
from router.main import get_pie_chart_gradient, TOOL_COLORS, stats


def test_get_pie_chart_gradient_empty():
    mock_stats = {"tool_tokens": {"tree": 0, "shell": 0, "write": 0, "view": 0, "other": 0}}
    with patch.dict(stats, mock_stats, clear=True):
        result = get_pie_chart_gradient()
        assert result == "background: rgba(255, 255, 255, 0.05);"


def test_get_pie_chart_gradient_single():
    mock_stats = {"tool_tokens": {"tree": 100, "shell": 0, "write": 0, "view": 0, "other": 0}}
    with patch.dict(stats, mock_stats, clear=True):
        result = get_pie_chart_gradient()
        color = TOOL_COLORS.get("tree", "#94a3b8")
        assert f"{color} 0.0% 100.0%" in result
        assert result.startswith("background: conic-gradient(")
        assert result.endswith(");")


def test_get_pie_chart_gradient_multiple():
    mock_stats = {"tool_tokens": {"tree": 50, "shell": 50, "write": 0, "view": 0, "other": 0}}
    with patch.dict(stats, mock_stats, clear=True):
        result = get_pie_chart_gradient()
        tree_color = TOOL_COLORS.get("tree", "#94a3b8")
        shell_color = TOOL_COLORS.get("shell", "#94a3b8")

        assert f"{tree_color} 0.0% 50.0%" in result
        assert f"{shell_color} 50.0% 100.0%" in result
        assert result.startswith("background: conic-gradient(")
        assert result.endswith(");")


def test_get_pie_chart_gradient_all():
    mock_stats = {"tool_tokens": {"tree": 10, "shell": 20, "write": 30, "view": 40, "other": 0}}
    with patch.dict(stats, mock_stats, clear=True):
        result = get_pie_chart_gradient()
        tree_color = TOOL_COLORS.get("tree", "#94a3b8")
        shell_color = TOOL_COLORS.get("shell", "#94a3b8")
        write_color = TOOL_COLORS.get("write", "#94a3b8")
        view_color = TOOL_COLORS.get("view", "#94a3b8")

        # total tokens = 10 + 20 + 30 + 40 = 100
        # tree: 10% (0.0% -> 10.0%)
        # shell: 20% (10.0% -> 30.0%)
        # write: 30% (30.0% -> 60.0%)
        # view: 40% (60.0% -> 100.0%)

        assert f"{tree_color} 0.0% 10.0%" in result
        assert f"{shell_color} 10.0% 30.0%" in result
        assert f"{write_color} 30.0% 60.0%" in result
        assert f"{view_color} 60.0% 100.0%" in result
        assert result.startswith("background: conic-gradient(")
        assert result.endswith(");")


def test_get_pie_chart_gradient_other_category():
    """Verify non-zero tokens in 'other' participate correctly with 'other' color in gradient."""
    mock_stats = {"tool_tokens": {"tree": 0, "shell": 0, "write": 0, "view": 0, "other": 100}}
    with patch.dict(stats, mock_stats, clear=True):
        result = get_pie_chart_gradient()
        other_color = TOOL_COLORS.get("other", "#f472b6")
        assert result == f"background: conic-gradient({other_color} 0.0% 100.0%);"


def test_get_pie_chart_gradient_all_tools_including_other():
    """Verify gradient when all tools including 'other' have non-zero tokens."""
    mock_stats = {"tool_tokens": {"tree": 10, "shell": 20, "write": 30, "view": 20, "other": 20}}
    with patch.dict(stats, mock_stats, clear=True):
        result = get_pie_chart_gradient()
        tree_color = TOOL_COLORS["tree"]
        shell_color = TOOL_COLORS["shell"]
        write_color = TOOL_COLORS["write"]
        view_color = TOOL_COLORS["view"]
        other_color = TOOL_COLORS["other"]

        # total tokens = 10 + 20 + 30 + 20 + 20 = 100
        assert f"{tree_color} 0.0% 10.0%" in result
        assert f"{shell_color} 10.0% 30.0%" in result
        assert f"{write_color} 30.0% 60.0%" in result
        assert f"{view_color} 60.0% 80.0%" in result
        assert f"{other_color} 80.0% 100.0%" in result
        assert result.startswith("background: conic-gradient(")
        assert result.endswith(");")


def test_get_pie_chart_gradient_unrecognized_tool():
    """Verify unrecognized tool uses the default fallback color (#94a3b8)."""
    mock_stats = {"tool_tokens": {"custom_tool": 100}}
    with patch.dict(stats, mock_stats, clear=True):
        result = get_pie_chart_gradient()
        assert result == "background: conic-gradient(#94a3b8 0.0% 100.0%);"
