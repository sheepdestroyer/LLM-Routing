import pytest
import sys
import os
from unittest.mock import patch

# Ensure router directory is in sys.path
router_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if router_path not in sys.path:
    sys.path.insert(0, router_path)

import main

def test_get_pie_chart_gradient_empty():
    mock_stats = {"tool_tokens": {"tree": 0, "shell": 0, "write": 0, "view": 0, "other": 0}}
    with patch.dict(main.stats, mock_stats, clear=True):
        result = main.get_pie_chart_gradient()
        assert result == "background: rgba(255, 255, 255, 0.05);"

def test_get_pie_chart_gradient_single():
    mock_stats = {"tool_tokens": {"tree": 100, "shell": 0, "write": 0, "view": 0, "other": 0}}
    with patch.dict(main.stats, mock_stats, clear=True):
        result = main.get_pie_chart_gradient()
        color = main.TOOL_COLORS.get("tree", "#94a3b8")
        assert f"{color} 0.0% 100.0%" in result
        assert result.startswith("background: conic-gradient(")
        assert result.endswith(");")

def test_get_pie_chart_gradient_multiple():
    mock_stats = {"tool_tokens": {"tree": 50, "shell": 50, "write": 0, "view": 0, "other": 0}}
    with patch.dict(main.stats, mock_stats, clear=True):
        result = main.get_pie_chart_gradient()
        tree_color = main.TOOL_COLORS.get("tree", "#94a3b8")
        shell_color = main.TOOL_COLORS.get("shell", "#94a3b8")

        assert f"{tree_color} 0.0% 50.0%" in result
        assert f"{shell_color} 50.0% 100.0%" in result
        assert result.startswith("background: conic-gradient(")
        assert result.endswith(");")

def test_get_pie_chart_gradient_all():
    mock_stats = {"tool_tokens": {"tree": 10, "shell": 20, "write": 30, "view": 40, "other": 0}}
    with patch.dict(main.stats, mock_stats, clear=True):
        result = main.get_pie_chart_gradient()
        tree_color = main.TOOL_COLORS.get("tree", "#94a3b8")
        shell_color = main.TOOL_COLORS.get("shell", "#94a3b8")
        write_color = main.TOOL_COLORS.get("write", "#94a3b8")
        view_color = main.TOOL_COLORS.get("view", "#94a3b8")

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
