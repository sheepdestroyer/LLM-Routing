import pytest
from router.main import src_badge

def test_src_badge():
    label = "TestLabel"
    color = "#ff0000"

    result = src_badge(label, color)

    # Check that label is present
    assert f">{label}</span>" in result

    # Check that color is correctly applied
    assert f"color: {color};" in result
    assert f"background: {color}18;" in result
    assert f"border: 1px solid {color}44;" in result

    # Check general structure
    assert result.startswith("<span")
    assert result.endswith("</span>")

@pytest.mark.parametrize("label, color", [
    ("A", "#123456"),
    ("Long Label", "red"),
    ("Label With Spaces", "blue"),
    ("", ""), # edge case empty strings
])
def test_src_badge_parameterized(label, color):
    result = src_badge(label, color)
    assert f">{label}</span>" in result
    assert f"color: {color};" in result
    assert f"background: {color}18;" in result
    assert f"border: 1px solid {color}44;" in result
