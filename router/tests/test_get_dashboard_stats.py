import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi import Request


@pytest.mark.asyncio
async def test_get_dashboard_stats():
    from router import main

    # Mock request
    mock_request = MagicMock(spec=Request)

    # Mock data returned by get_dashboard_data
    mock_data = {"key": "value"}

    # Mock template rendering
    mock_template = MagicMock()
    mock_template.render.return_value = "<div>Mocked HTML</div>"

    with (
        patch("router.main.get_dashboard_data", new_callable=AsyncMock) as mock_get_dashboard_data,
        patch("router.main.templates.get_template") as mock_get_template,
    ):
        mock_get_dashboard_data.return_value = mock_data
        mock_get_template.return_value = mock_template

        result = await main.get_dashboard_stats(mock_request)

        # Assert get_dashboard_data was called
        mock_get_dashboard_data.assert_called_once()

        # Assert templates.get_template was called for each partial
        expected_partials = [
            "partials/oauth_banner.html",
            "partials/tier_table.html",
            "partials/pie_legend.html",
            "partials/tool_tokens.html",
            "partials/routing_legend.html",
            "partials/timeline.html",
            "partials/goose.html",
            "partials/llamacpp_models.html",
            "partials/llamacpp_slots.html",
        ]

        for partial in expected_partials:
            mock_get_template.assert_any_call(partial)

        assert mock_get_template.call_count == len(expected_partials)

        # Assert render was called with correct context
        expected_context = {"request": mock_request, "data": mock_data}
        mock_template.render.assert_called_with(expected_context)

        # Assert result contains the original data and the HTML keys
        assert result["key"] == "value"
        assert result["oauth_banner_html"] == "<div>Mocked HTML</div>"
        assert result["tier_table_html"] == "<div>Mocked HTML</div>"
        assert result["pie_legend_html"] == "<div>Mocked HTML</div>"
        assert result["tool_tokens_html"] == "<div>Mocked HTML</div>"
        assert result["routing_legend_html"] == "<div>Mocked HTML</div>"
        assert result["timeline_html"] == "<div>Mocked HTML</div>"
        assert result["goose_html"] == "<div>Mocked HTML</div>"
        assert result["llamacpp_models_html"] == "<div>Mocked HTML</div>"
        assert result["llamacpp_slots_html"] == "<div>Mocked HTML</div>"
