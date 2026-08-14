import re
import pytest
from playwright.async_api import Page, expect


@pytest.mark.anyio
async def test_dashboard_title_and_layout(page: Page, base_url: str):
    """Verify that the dashboard loads successfully with proper title, structure, and widgets."""
    await page.goto(f"{base_url}/dashboard")

    # Title check
    await expect(page).to_have_title("LLM Triage Gateway - Control Center")

    # Header check
    header = page.locator("header")
    await expect(header.locator(".logo-text")).to_contain_text("Antigravity Gateway")
    await expect(header.locator(".dashboard-title")).to_contain_text("System Control Center")

    # Dataset Visualizer navigation link check
    vis_link = page.locator("#visualizer-link")
    await expect(vis_link).to_be_visible()
    await expect(vis_link).to_contain_text("Dataset Visualizer")

    # System Health Map services check
    service_names = await page.locator(".service-name").all_text_contents()
    expected_services = [
        "Triage Router",
        "LiteLLM Proxy",
        "Valkey Cache",
        "Llama-Server",
        "Langfuse Traces",
    ]
    for s in expected_services:
        assert any(s in name for name in service_names), f"Missing service {s} in health map"

    # Metric card labels
    metric_labels = await page.locator(".metric-label").all_text_contents()
    assert any("Total API Calls" in label for label in metric_labels)
    assert any("Last Triage Split" in label for label in metric_labels)
    assert any("Avg Triage Time" in label for label in metric_labels)
    assert any("Avg Proxy Time" in label for label in metric_labels)
    assert any("Triage Cache Hits" in label for label in metric_labels)

    # Token usage tracker card
    await expect(page.locator("#p-tokens")).to_be_visible()
    await expect(page.locator("#c-tokens")).to_be_visible()
    await expect(page.locator("#t-tokens")).to_be_visible()

    # Footer verification
    footer = page.locator("footer")
    await expect(footer).to_contain_text("LLM Triage Gateway Control Center")


@pytest.mark.anyio
async def test_dashboard_live_polling_dom_updates(page: Page, base_url: str):
    """Test that periodic stats polling dynamically updates DOM metrics and status badges."""
    mock_stats = {
        "litellm_status": True,
        "valkey_status": True,
        "llama_server_status": True,
        "langfuse_status": True,
        "total_requests": 8844,
        "last_triage_decision": "agent-reasoning-core",
        "avg_triage_latency_ms": 14.2,
        "avg_proxy_latency_ms": 38.5,
        "cache_hits": 512,
        "p_tokens": 120500,
        "c_tokens": 84200,
        "t_tokens": 204700,
        "oauth_banner_html": "<div class='oauth-banner-valid'>OAuth Token Active</div>",
        "tier_table_html": "<table><tbody><tr><td>Tier Simple</td><td>100</td></tr></tbody></table>",
        "pie_legend_html": "<div class='mock-legend'>Legend Data</div>",
        "routing_legend_html": "<div class='mock-routing'>Routing Data</div>",
        "tool_tokens_html": "<div class='mock-tools'>Tool Tokens</div>",
        "timeline_html": "<div class='mock-timeline'>Timeline Activity</div>",
        "goose_html": "<div class='mock-goose'>Active Goose Session #1</div>",
        "llamacpp_models_html": "<div class='mock-models'>Model: gemma-4b</div>",
        "llamacpp_slots_html": "<div class='mock-slots'>Slot: Idle</div>",
        "best_free_model": {
            "id": "openrouter/meta-llama-3.3-70b-instruct:free",
            "name": "Meta Llama 3.3 70B",
            "score": 94.8,
            "context_length": 131072,
            "is_fallback": False,
        },
        "llamacpp_build": "3499",
        "pie_gradient": "background: conic-gradient(#34d399 0% 100%);",
        "routing_gradient": "background: conic-gradient(#818cf8 0% 100%);",
    }

    async def handle_stats_route(route):
        await route.fulfill(
            status=200,
            content_type="application/json",
            json=mock_stats,
        )

    await page.route("**/api/dashboard-stats*", handle_stats_route)

    await page.goto(f"{base_url}/dashboard")

    # Manually invoke refreshStats() or wait for DOM population
    await page.evaluate("refreshStats()")

    # Verify updated values in DOM
    await expect(page.locator("#total-requests")).to_have_text("8844")
    await expect(page.locator("#last-triage-decision")).to_have_text("agent-reasoning-core")
    await expect(page.locator("#avg-triage-latency")).to_have_text("14.2 ms")
    await expect(page.locator("#avg-proxy-latency")).to_have_text("38.5 ms")
    await expect(page.locator("#cache-hits")).to_have_text("512")

    await expect(page.locator("#p-tokens")).to_have_text("120,500")
    await expect(page.locator("#c-tokens")).to_have_text("84,200")
    await expect(page.locator("#t-tokens")).to_have_text("204,700")

    # Status badges
    await expect(page.locator("#litellm-status")).to_have_class(re.compile(r"badge-online"))
    await expect(page.locator("#valkey-status")).to_have_class(re.compile(r"badge-online"))
    await expect(page.locator("#llama-server-status")).to_have_class(re.compile(r"badge-online"))
    await expect(page.locator("#langfuse-status")).to_have_class(re.compile(r"badge-online"))

    # Llama build & Free Model widget
    await expect(page.locator("#llamacpp-build")).to_have_text("build 3499")
    await expect(page.locator("#best-free-model-container")).to_contain_text("Meta Llama 3.3 70B")
    await expect(page.locator("#best-free-model-container")).to_contain_text("94.8")
    await expect(page.locator("#best-free-model-container")).to_contain_text("LIVE")


@pytest.mark.anyio
async def test_dashboard_offline_service_status_badges(page: Page, base_url: str):
    """Test that offline services reflect badge-offline classes and Offline text."""
    mock_stats = {
        "litellm_status": False,
        "valkey_status": False,
        "llama_server_status": False,
        "langfuse_status": False,
        "total_requests": 0,
        "last_triage_decision": "None",
        "avg_triage_latency_ms": 0.0,
        "avg_proxy_latency_ms": 0.0,
        "cache_hits": 0,
        "p_tokens": 0,
        "c_tokens": 0,
        "t_tokens": 0,
        "oauth_banner_html": "",
        "tier_table_html": "",
        "pie_legend_html": "",
        "routing_legend_html": "",
        "tool_tokens_html": "",
        "timeline_html": "",
        "goose_html": "",
        "llamacpp_models_html": "",
        "llamacpp_slots_html": "",
        "best_free_model": {
            "id": "none",
            "name": "Fallback Model",
            "score": 0.0,
            "context_length": 4096,
            "is_fallback": True,
        },
        "llamacpp_build": "unknown",
        "pie_gradient": "",
        "routing_gradient": "",
    }

    await page.route("**/api/dashboard-stats*", lambda route: route.fulfill(
        status=200, content_type="application/json", json=mock_stats
    ))

    await page.goto(f"{base_url}/dashboard")
    await page.evaluate("refreshStats()")

    await expect(page.locator("#litellm-status")).to_have_class(re.compile(r"badge-offline"))
    await expect(page.locator("#litellm-status")).to_contain_text("Offline")

    await expect(page.locator("#valkey-status")).to_have_class(re.compile(r"badge-offline"))
    await expect(page.locator("#valkey-status")).to_contain_text("Offline")

    await expect(page.locator("#llama-server-status")).to_have_class(re.compile(r"badge-offline"))
    await expect(page.locator("#llama-server-status")).to_contain_text("Offline")

    await expect(page.locator("#langfuse-status")).to_have_class(re.compile(r"badge-offline"))
    await expect(page.locator("#langfuse-status")).to_contain_text("Offline")

    # Fallback model badge
    await expect(page.locator("#best-free-model-container")).to_contain_text("FALLBACK")


@pytest.mark.anyio
async def test_dashboard_pie_chart_gradients(page: Page, base_url: str):
    """Test that pie charts update their styles when gradients are received."""
    mock_stats = {
        "litellm_status": True,
        "valkey_status": True,
        "llama_server_status": True,
        "langfuse_status": True,
        "total_requests": 10,
        "last_triage_decision": "agent-simple-core",
        "avg_triage_latency_ms": 5.0,
        "avg_proxy_latency_ms": 10.0,
        "cache_hits": 2,
        "p_tokens": 100,
        "c_tokens": 50,
        "t_tokens": 150,
        "oauth_banner_html": "",
        "tier_table_html": "",
        "pie_legend_html": "",
        "routing_legend_html": "",
        "tool_tokens_html": "",
        "timeline_html": "",
        "goose_html": "",
        "llamacpp_models_html": "",
        "llamacpp_slots_html": "",
        "best_free_model": {
            "id": "model",
            "name": "Model",
            "score": 80.0,
            "context_length": 8192,
            "is_fallback": False,
        },
        "llamacpp_build": "3500",
        "pie_gradient": "background: conic-gradient(rgb(52, 211, 153) 0%, rgb(52, 211, 153) 100%);",
        "routing_gradient": "background: conic-gradient(rgb(129, 140, 248) 0%, rgb(129, 140, 248) 100%);",
    }

    await page.route("**/api/dashboard-stats*", lambda route: route.fulfill(
        status=200, content_type="application/json", json=mock_stats
    ))

    await page.goto(f"{base_url}/dashboard")
    await page.evaluate("refreshStats()")

    tool_pie = page.locator("#tool-token-pie-chart")
    await expect(tool_pie).to_be_visible()
    style = (await tool_pie.get_attribute("style")) or ""
    assert "conic-gradient" in style


@pytest.mark.anyio
async def test_dashboard_quick_links_accessibility_and_security(page: Page, base_url: str):
    """Verify that external console links have security attributes (target, rel) and accessible descriptions."""
    await page.goto(f"{base_url}/dashboard")

    links = await page.locator(".status-container .btn-group a").all()
    assert len(links) >= 3, "Expected at least 3 console quick links"

    for link in links:
        target = await link.get_attribute("target")
        rel = await link.get_attribute("rel")
        aria_describedby = await link.get_attribute("aria-describedby")
        inner_text = await link.inner_text()

        assert target == "_blank", f"Link {inner_text} missing target='_blank'"
        assert "noopener" in (rel or "") and "noreferrer" in (rel or ""), f"Link {inner_text} missing secure rel attribute"
        assert aria_describedby == "new-tab-desc", f"Link {inner_text} missing aria-describedby for accessibility"


@pytest.mark.anyio
async def test_dashboard_responsive_mobile_viewport(page: Page, base_url: str):
    """Verify that the dashboard renders without critical errors on mobile screens."""
    await page.set_viewport_size({"width": 375, "height": 667})
    await page.goto(f"{base_url}/dashboard")

    # Ensure main container and metrics are still visible
    await expect(page.locator("header")).to_be_visible()
    await expect(page.locator(".metrics-grid")).to_be_visible()
    await expect(page.locator("footer")).to_be_visible()
