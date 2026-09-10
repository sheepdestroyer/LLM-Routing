from pathlib import Path
import pytest
from unittest.mock import patch, MagicMock
from fastapi.responses import HTMLResponse
from router.main import get_visualizer


@pytest.mark.anyio
async def test_get_visualizer_exists():
    with patch("router.main.STATIC_DIR") as mock_static_dir:
        mock_vis_path = MagicMock()
        mock_vis_path.exists.return_value = True
        mock_vis_path.read_text.return_value = "<html>Visualizer Content</html>"
        mock_static_dir.__truediv__.return_value = mock_vis_path

        response = await get_visualizer()

        assert isinstance(response, HTMLResponse)
        assert response.status_code == 200
        assert response.body == b"<html>Visualizer Content</html>"


@pytest.mark.anyio
async def test_get_visualizer_not_found():
    with patch("router.main.STATIC_DIR") as mock_static_dir:
        mock_vis_path = MagicMock()
        mock_vis_path.exists.return_value = False
        mock_static_dir.__truediv__.return_value = mock_vis_path

        response = await get_visualizer()

        assert isinstance(response, HTMLResponse)
        assert response.status_code == 404
        assert response.body == b"<h2>Visualizer not found</h2>"


def test_visualizer_html_clear_button_accessibility():
    """Verify visualizer.html uses semantic button with .clear-btn class instead of anchor tag for clear action."""
    vis_path = Path(__file__).resolve().parent.parent / "static" / "visualizer.html"
    assert vis_path.exists(), f"File not found: {vis_path}"
    content = vis_path.read_text(encoding="utf-8")

    # Verify semantic button element
    assert '<button type="button" onclick="clearAnnotation(${idx})" class="clear-btn"' in content
    # Verify legacy anchor tag with hash navigation is removed
    assert '<a href="#" onclick="clearAnnotation' not in content
    # Verify CSS styling for .clear-btn with focus-visible accessibility
    assert ".clear-btn {" in content
    assert ".clear-btn:focus-visible {" in content
