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
