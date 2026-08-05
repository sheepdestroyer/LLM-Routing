import pytest
import markupsafe
from router.main import src_badge


@pytest.mark.parametrize(
    "label, color",
    [
        ("ROUTER", "#818cf8"),
        ("LITELLM", "#34d399"),
        ("GOOSE", "#fbbf24"),
        ("LLAMA.CPP", "#fb923c"),
        ("LANGFUSE", "#e879f9"),
        ("A", "#123456"),
        ("Long Label", "#ff0000"),
        ("Label With Spaces", "#0000ff"),
        ("O'Connor & Sons", "#123456"),
        ("", "#000000"),
    ],
)
def test_src_badge(label: str, color: str) -> None:
    """Test src_badge generates correct HTML span with proper hex styling."""
    result = src_badge(label, color)
    safe_label = markupsafe.escape(label)
    safe_color = markupsafe.escape(color)
    assert result.startswith("<span")
    assert result.endswith("</span>")
    assert f">{safe_label}</span>" in result
    assert f"color: {safe_color};" in result
    assert f"background: {safe_color}18;" in result
    assert f"border: 1px solid {safe_color}44;" in result
