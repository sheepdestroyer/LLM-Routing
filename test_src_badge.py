import pytest
from router.main import src_badge

def test_src_badge_typical():
    """Test src_badge with typical label and color."""
    label = "Active"
    color = "#00FF00"
    result = src_badge(label, color)

    assert isinstance(result, str)
    assert "<span" in result
    assert f">{label}</span>" in result
    assert f"color: {color};" in result
    assert f"background: {color}18;" in result
    assert f"border: 1px solid {color}44;" in result

def test_src_badge_empty_label():
    """Test src_badge with empty label."""
    label = ""
    color = "#FF0000"
    result = src_badge(label, color)

    assert isinstance(result, str)
    assert "></span>" in result
    assert f"color: {color};" in result

def test_src_badge_special_chars():
    """Test src_badge with special characters in label."""
    label = "O'Connor & Sons"
    color = "blue"
    result = src_badge(label, color)

    assert isinstance(result, str)
    assert f">{label}</span>" in result
    assert "color: blue;" in result

def test_src_badge_exact_html():
    """Test the exact output HTML string matches the expected template."""
    label = "Test"
    color = "red"
    expected = "<span style='font-size: 9px; padding: 2px 7px; border-radius: 4px; background: red18; color: red; border: 1px solid red44; font-weight: 700; letter-spacing: 0.5px; vertical-align: middle; margin-right: 8px;'>Test</span>"
    result = src_badge(label, color)

    assert result == expected

if __name__ == "__main__":
    pytest.main(["-v", "test_src_badge.py"])
