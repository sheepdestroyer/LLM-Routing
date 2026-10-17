"""Real headless Chromium UI tests with synthetic API routes, not live API qualification.

Render the shipped Jinja dashboard without starting the application or its backends.
All browser networking is intercepted, so no production credentials/services are used.
"""

import copy
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import expect, sync_playwright

ORIGIN = "https://dashboard.synthetic.invalid"
KEY = "synthetic-admin-token-not-a-real-key"
REPORT: dict[str, Any] = {
    "status": "ok",
    "results": {"created": 0, "updated": 0, "failed": 0},
    "metadata": {
        "dry_run": False,
        "counts": {"updated": 2, "unchanged": 1, "unresolved": 0, "failed": 0},
        "errors": [],
        "deployments": [
            {"id": "synthetic-a", "provenance": {"overrides": ["model_info.max_tokens"], "unresolved": []}},
            {
                "id": "synthetic-b",
                "provenance": {"overrides": ["litellm_params.input_cost_per_token"], "unresolved": []},
            },
        ],
    },
}


@pytest.fixture(scope="module")
def rendered_dashboard():
    templates = Path(__file__).resolve().parents[1] / "templates"
    env = Environment(loader=FileSystemLoader(templates), autoescape=True)
    data: dict[str, object] = dict.fromkeys(
        (
            "total_requests",
            "avg_triage_latency_ms",
            "avg_proxy_latency_ms",
            "cache_hits",
            "p_tokens",
            "c_tokens",
            "t_tokens",
        ),
        0,
    )
    data.update(
        best_free_model=None,
        last_triage_decision="Synthetic fixture",
        oauth_status={"status": "valid", "detail": "Synthetic fixture"},
        llamacpp={"models": [], "slots": []},
    )
    return env.get_template("dashboard.html").render(data=data, src_badge=lambda *args: "")


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def synthetic_ui(chromium, rendered_dashboard):
    context = chromium.new_context()
    page = context.new_page()
    page.set_default_timeout(5000)
    requests = []
    console = []
    page.on("console", lambda message: console.append(message.text))

    def intercept(route):
        request = route.request
        requests.append(request)
        if request.resource_type == "document":
            route.fulfill(content_type="text/html", body=rendered_dashboard)
        else:
            route.fulfill(status=404, body="Synthetic fixture: no resource")

    page.route("**/*", intercept)
    yield page, requests, console
    context.close()


def open_dashboard(ui, path="/dashboard"):
    page, requests, _ = ui
    page.goto(ORIGIN + path)
    expect(page.get_by_role("button", name="Refresh model metadata", exact=True)).to_be_visible()
    assert not [request for request in requests if "/admin/" in request.url]
    return page


def submit(page):
    page.get_by_label("Admin key (ROUTER_API_KEY or LITELLM_MASTER_KEY)").fill(KEY)
    page.get_by_role("button", name="Refresh model metadata", exact=True).click()


def assert_cleared(ui):
    page, _, console = ui
    expect(page.locator("#metadata-admin-key")).to_have_value("")
    expect(page.locator("#metadata-admin-key")).to_be_enabled()
    expect(page.locator("#metadata-refresh-button")).to_be_enabled()
    expect(page.locator("#metadata-refresh-form")).to_have_attribute("aria-busy", "false")
    assert page.evaluate("({local: localStorage.length, session: sessionStorage.length, cookie: document.cookie})") == {
        "local": 0,
        "session": 0,
        "cookie": "",
    }
    assert KEY not in page.content()
    assert not any(KEY in message for message in console)


@pytest.mark.parametrize("path", ["/dashboard", "/dashboard/", "/prefix/dashboard", "/prefix/dashboard/"])
def test_success_same_origin_metadata_only_and_transient_key(synthetic_ui, path):
    page = open_dashboard(synthetic_ui, path)
    observed = []

    def respond(route):
        observed.append(route.request)
        assert page.locator("#metadata-admin-key").input_value() == ""
        route.fulfill(json=REPORT)

    page.route("**/admin/sync-models?*", respond)
    expect(page.locator("#metadata-admin-key")).to_have_attribute("type", "password")
    expect(page.locator("#metadata-admin-key")).to_have_attribute("autocomplete", "off")
    expect(page.locator("#metadata-refresh-result")).to_have_attribute("aria-live", "polite")
    submit(page)
    expect(page.locator("#metadata-refresh-result")).to_have_attribute("data-state", "success")
    expect(page.locator("#metadata-refresh-result")).to_have_text(
        "Metadata refresh complete. Updated: 2; unchanged: 1; unresolved: 0; failed: 0; overrides preserved: 2."
    )
    assert len(observed) == 1
    request = observed[0]
    prefix = "/prefix" if path.startswith("/prefix/") else ""
    assert urlsplit(request.url).path == prefix + "/admin/sync-models"
    assert request.url.startswith(ORIGIN + "/")
    assert parse_qs(urlsplit(request.url).query) == {"metadata_only": ["true"], "force_catalog_refresh": ["true"]}
    assert request.method == "POST"
    assert request.headers["authorization"] == "Bearer " + KEY
    assert not request.post_data
    assert KEY not in request.url
    assert_cleared(synthetic_ui)
    # Repeated manual changes require a newly supplied key; never reuse it.
    page.locator("#metadata-refresh-button").click()
    expect(page.locator("#metadata-refresh-result")).to_contain_text("Enter an admin key")
    assert len(observed) == 1


@pytest.mark.parametrize("failure", ["failed", "unresolved", "catalog", "cached_catalog", "scan"])
def test_partial_or_catalog_failure_is_warning_not_success(synthetic_ui, failure):
    page = open_dashboard(synthetic_ui)
    report = copy.deepcopy(REPORT)
    if failure in ("failed", "unresolved"):
        report["metadata"]["counts"][failure] = 1
    elif failure == "catalog":
        report["metadata"]["errors"] = ["catalog:RuntimeError"]
    elif failure == "cached_catalog":
        report["metadata"]["counts"]["unresolved"] = 1
        report["metadata"]["deployments"][0]["provenance"]["unresolved"] = ["catalog_unavailable"]
    else:
        report["metadata"]["errors"] = ["deployment_scan:HTTPStatusError"]
    page.route("**/admin/sync-models?*", lambda route: route.fulfill(json=report))
    submit(page)
    expect(page.locator("#metadata-refresh-result")).to_have_attribute("data-state", "warning")
    expect(page.locator("#metadata-refresh-result")).to_contain_text("Completed with warnings")
    expect(page.locator("#metadata-refresh-result")).to_contain_text("overrides preserved: 2")
    if failure in ("failed", "unresolved"):
        expect(page.locator("#metadata-refresh-result")).to_contain_text(f"{failure}: 1")
    if failure in ("catalog", "cached_catalog"):
        expect(page.locator("#metadata-refresh-result")).to_contain_text("Catalog unavailable")
        expect(page.locator("#metadata-refresh-result")).to_contain_text("Server retry backoff")
    assert_cleared(synthetic_ui)


@pytest.mark.parametrize("status", [401, 403, 500])
def test_http_errors_do_not_echo_server_secrets(synthetic_ui, status):
    page = open_dashboard(synthetic_ui)
    page.route("**/admin/sync-models?*", lambda route: route.fulfill(status=status, json={"detail": KEY}))
    submit(page)
    result = page.locator("#metadata-refresh-result")
    expect(result).to_have_attribute("data-state", "error")
    expect(result).to_contain_text(str(status))
    if status in (401, 403):
        expect(result).to_contain_text("Authentication failed")
    assert_cleared(synthetic_ui)


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "null",
        "{}",
        '{"status":"ok","metadata":{"counts":{"updated":-1}}}',
        json.dumps({**REPORT, "metadata": {**REPORT["metadata"], "deployments": [{"provenance": {"overrides": KEY}}]}}),
    ],
)
def test_malformed_response_is_safe_error(synthetic_ui, body):
    page = open_dashboard(synthetic_ui)
    page.route("**/admin/sync-models?*", lambda route: route.fulfill(content_type="application/json", body=body))
    submit(page)
    expect(page.locator("#metadata-refresh-result")).to_have_attribute("data-state", "error")
    expect(page.locator("#metadata-refresh-result")).to_contain_text("Invalid metadata refresh response")
    assert_cleared(synthetic_ui)


def test_network_failure(synthetic_ui):
    page = open_dashboard(synthetic_ui)
    page.route("**/admin/sync-models?*", lambda route: route.abort("failed"))
    submit(page)
    expect(page.locator("#metadata-refresh-result")).to_contain_text("Network error")
    assert_cleared(synthetic_ui)


@pytest.mark.parametrize("key", ["", "   "])
def test_missing_key_sends_no_admin_http(synthetic_ui, key):
    page = open_dashboard(synthetic_ui)
    page.locator("#metadata-admin-key").fill(key)
    page.locator("#metadata-refresh-button").click()
    expect(page.locator("#metadata-refresh-result")).to_contain_text("Enter an admin key")
    assert not [request for request in synthetic_ui[1] if "/admin/" in request.url]
    assert_cleared(synthetic_ui)


def test_busy_double_submit_and_bounded_timeout(synthetic_ui):
    page = open_dashboard(synthetic_ui)
    page.clock.install()
    held = []
    page.route("**/admin/sync-models?*", lambda route: held.append(route))
    submit(page)
    expect(page.locator("#metadata-refresh-button")).to_be_disabled()
    expect(page.locator("#metadata-admin-key")).to_have_value("")
    expect(page.locator("#metadata-admin-key")).to_be_disabled()
    expect(page.locator("#metadata-refresh-form")).to_have_attribute("aria-busy", "true")
    expect(page.locator("#metadata-refresh-result")).to_have_attribute("data-state", "busy")
    # A duplicate event bypassing button disabling is still guarded.
    page.locator("#metadata-refresh-button").dispatch_event("click")
    page.locator("#metadata-refresh-form").dispatch_event("submit")
    page.clock.fast_forward(120001)
    expect(page.locator("#metadata-refresh-result")).to_contain_text("timed out after 120 seconds")
    expect(page.locator("#metadata-refresh-result")).to_contain_text("server may still be running")
    assert len(held) == 1
    assert_cleared(synthetic_ui)
    assert not [request for request in synthetic_ui[1] if "/admin/" in request.url]
    # Close the intercepted request after the browser aborted it.
    held[0].abort()


def test_pending_success_prevents_duplicates_and_does_not_poll_admin(synthetic_ui):
    page = open_dashboard(synthetic_ui)
    page.clock.install()
    held = []
    page.route("**/admin/sync-models?*", lambda route: held.append(route))
    submit(page)
    expect(page.locator("#metadata-refresh-form")).to_have_attribute("aria-busy", "true")
    page.locator("#metadata-refresh-button").dispatch_event("click")
    page.locator("#metadata-refresh-form").dispatch_event("submit")
    page.clock.fast_forward(10001)
    assert len(held) == 1
    assert not [request for request in synthetic_ui[1] if "/admin/" in request.url]
    held[0].fulfill(json=REPORT)
    expect(page.locator("#metadata-refresh-result")).to_have_attribute("data-state", "success")
    assert_cleared(synthetic_ui)
    page.clock.fast_forward(120001)
    expect(page.locator("#metadata-refresh-result")).to_have_attribute("data-state", "success")
    assert len(held) == 1


def test_untrusted_report_text_is_never_inserted_or_logged(synthetic_ui):
    page = open_dashboard(synthetic_ui)
    report = copy.deepcopy(REPORT)
    report["metadata"]["errors"] = [KEY + '<img src=x onerror="window.injected=true">']
    report["metadata"]["deployments"][0]["id"] = KEY
    report["metadata"]["deployments"][0]["provenance"]["overrides"] = [KEY]
    page.route("**/admin/sync-models?*", lambda route: route.fulfill(json=report))
    submit(page)
    expect(page.locator("#metadata-refresh-result")).to_have_attribute("data-state", "warning")
    assert page.locator("#metadata-refresh-result img").count() == 0
    assert page.evaluate("window.injected") is None
    assert_cleared(synthetic_ui)


def test_redirect_does_not_send_key_cross_origin(synthetic_ui):
    page = open_dashboard(synthetic_ui)
    escaped = []
    page.route("https://other.synthetic.invalid/**", lambda route: (escaped.append(route.request), route.abort()))
    page.route(
        "**/admin/sync-models?*",
        lambda route: route.fulfill(
            status=307, headers={"location": "https://other.synthetic.invalid/admin/sync-models"}
        ),
    )
    submit(page)
    expect(page.locator("#metadata-refresh-result")).to_contain_text("Network error")
    assert escaped == []
    assert_cleared(synthetic_ui)
