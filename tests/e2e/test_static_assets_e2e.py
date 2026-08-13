import pytest
from playwright.async_api import Page


@pytest.mark.anyio
async def test_favicon_ico_and_svg_assets(page: Page, base_url: str):
    """Verify that favicon.ico and favicon.svg are served correctly with proper status and content types."""
    # Test favicon.svg
    svg_response = await page.request.get(f"{base_url}/static/favicon.svg")
    assert svg_response.status == 200
    assert "svg" in svg_response.headers.get("content-type", "")

    # Test favicon.ico endpoint
    ico_response = await page.request.get(f"{base_url}/favicon.ico")
    assert ico_response.status == 200
    assert any(
        t in ico_response.headers.get("content-type", "")
        for t in ["image/x-icon", "image/vnd.microsoft.icon"]
    )


@pytest.mark.anyio
async def test_visualizer_html_served_via_static_and_route(page: Page, base_url: str):
    """Verify that the visualizer HTML is served both at /visualizer and /static/visualizer.html."""
    resp1 = await page.request.get(f"{base_url}/visualizer")
    assert resp1.status == 200
    assert "text/html" in resp1.headers.get("content-type", "")

    resp2 = await page.request.get(f"{base_url}/static/visualizer.html")
    assert resp2.status == 200
    assert "text/html" in resp2.headers.get("content-type", "")


@pytest.mark.anyio
async def test_nonexistent_static_asset_404(page: Page, base_url: str):
    """Verify that requesting a missing static file returns a clean 404."""
    response = await page.request.get(f"{base_url}/static/nonexistent_file.png")
    assert response.status == 404
