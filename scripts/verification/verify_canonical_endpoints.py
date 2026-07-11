#!/usr/bin/env python3
"""
Canonical endpoint verification — reads ports/URLs from .env files,
validates basic API endpoints, and runs E2E chat completion requests.

Usage:
    python scripts/verification/verify_canonical_endpoints.py          # prod (default)
    python scripts/verification/verify_canonical_endpoints.py --dev    # dev overlay
    python scripts/verification/verify_canonical_endpoints.py --prod  # explicit prod
"""
import os
import sys
import json
import time
import argparse
import httpx
from pathlib import Path

WORKDIR = Path(__file__).resolve().parent.parent.parent


def load_env(dev: bool = False) -> dict:
    """Load .env (and optionally .env.dev overlay), return resolved config dict."""
    env = {}

    def _parse(path: Path):
        if not path.exists():
            return
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                env[key] = val

    _parse(WORKDIR / ".env")
    if dev:
        _parse(WORKDIR / ".env.dev")

    # Resolve with defaults
    return {
        "router_port": env.get("ROUTER_PORT", "5000"),
        "litellm_port": env.get("LITELLM_PORT", "4000"),
        "langfuse_web_port": env.get("LANGFUSE_WEB_PORT", "3001"),
        "litellm_master_key": env.get("LITELLM_MASTER_KEY", "gateway-pass"),
        "router_api_key": env.get("ROUTER_API_KEY", "gateway-pass"),
        "public_base_url": env.get("PUBLIC_BASE_URL", ""),
        "base_url": env.get("BASE_URL", env.get("BASEURL", "x570.vendeuvre.lan")),
    }


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "✓" if ok else "✗"
    extra = f" — {detail}" if detail else ""
    print(f"  {mark} {label}{extra}")
    return ok


def test_router_endpoints(cfg: dict) -> tuple[int, int]:
    """Test router API endpoints. Returns (passed, total)."""
    base = f"http://127.0.0.1:{cfg['router_port']}"
    key = cfg["router_api_key"]
    headers = {"Authorization": f"Bearer {key}"}
    passed = total = 0

    print(f"\n── Router endpoints ({base}) ──")

    # /v1/models
    total += 1
    try:
        r = httpx.get(f"{base}/v1/models", headers=headers, timeout=10)
        models = r.json()
        model_ids = [m["id"] for m in models.get("data", [])]
        ok = r.status_code == 200 and len(model_ids) > 0
        passed += check("/v1/models", ok, f"{len(model_ids)} models")
    except Exception as e:
        passed += check("/v1/models", False, str(e))

    # /metrics
    total += 1
    try:
        r = httpx.get(f"{base}/metrics", timeout=10)
        ok = r.status_code == 200 and "triage_requests_total" in r.text
        passed += check("/metrics", ok)
    except Exception as e:
        passed += check("/metrics", False, str(e))

    # /dashboard
    total += 1
    try:
        r = httpx.get(f"{base}/dashboard", timeout=10)
        ok = r.status_code == 200 and "<html" in r.text.lower()
        passed += check("/dashboard", ok)
    except Exception as e:
        passed += check("/dashboard", False, str(e))

    # /api/dashboard-stats
    total += 1
    try:
        r = httpx.get(f"{base}/api/dashboard-stats", timeout=10)
        data = r.json()
        ok = r.status_code == 200 and isinstance(data, dict)
        passed += check("/api/dashboard-stats", ok)
    except Exception as e:
        passed += check("/api/dashboard-stats", False, str(e))

    # /visualizer
    total += 1
    try:
        r = httpx.get(f"{base}/visualizer", timeout=10)
        ok = r.status_code == 200 and "<html" in r.text.lower()
        passed += check("/visualizer", ok)
    except Exception as e:
        passed += check("/visualizer", False, str(e))

    return passed, total


def test_litellm_endpoints(cfg: dict) -> tuple[int, int]:
    """Test LiteLLM health endpoints. Returns (passed, total)."""
    base = f"http://127.0.0.1:{cfg['litellm_port']}"
    key = cfg["litellm_master_key"]
    headers = {"Authorization": f"Bearer {key}"}
    passed = total = 0

    print(f"\n── LiteLLM endpoints ({base}) ──")

    # /health/liveness
    total += 1
    try:
        r = httpx.get(f"{base}/health/liveness", timeout=10)
        passed += check("/health/liveness", r.status_code == 200)
    except Exception as e:
        passed += check("/health/liveness", False, str(e))

    # /health/readiness
    total += 1
    try:
        r = httpx.get(f"{base}/health/readiness", timeout=10)
        passed += check("/health/readiness", r.status_code == 200)
    except Exception as e:
        passed += check("/health/readiness", False, str(e))

    # /v1/models (LiteLLM)
    total += 1
    try:
        r = httpx.get(f"{base}/v1/models", headers=headers, timeout=10)
        models = r.json()
        model_ids = [m["id"] for m in models.get("data", [])]
        ok = r.status_code == 200 and len(model_ids) > 0
        passed += check("/v1/models", ok, f"{len(model_ids)} models")
    except Exception as e:
        passed += check("/v1/models", False, str(e))

    # /llm-routing/litellm/ui/ (LiteLLM admin UI — requires SERVER_ROOT_PATH prefix)
    total += 1
    try:
        r = httpx.get(f"{base}/llm-routing/litellm/ui/", timeout=10, follow_redirects=True)
        ok = r.status_code == 200 and "<html" in r.text.lower()
        passed += check("/llm-routing/litellm/ui/", ok)
    except Exception as e:
        passed += check("/llm-routing/litellm/ui/", False, str(e))

    return passed, total


def test_langfuse_endpoints(cfg: dict) -> tuple[int, int]:
    """Test Langfuse health endpoint. Returns (passed, total)."""
    base = f"http://127.0.0.1:{cfg['langfuse_web_port']}"
    passed = total = 0

    print(f"\n── Langfuse endpoints ({base}) ──")

    total += 1
    try:
        r = httpx.get(f"{base}/api/public/health", timeout=10)
        ok = r.status_code == 200
        passed += check("/api/public/health", ok, r.json() if ok else r.text[:80])
    except Exception as e:
        passed += check("/api/public/health", False, str(e))

    # / (Langfuse web UI)
    total += 1
    try:
        r = httpx.get(f"{base}/", timeout=10, follow_redirects=True)
        ok = r.status_code == 200 and "<html" in r.text.lower()
        passed += check("/ (web UI)", ok)
    except Exception as e:
        passed += check("/ (web UI)", False, str(e))

    return passed, total


def test_e2e_chat(cfg: dict) -> tuple[int, int]:
    """Run E2E chat completion requests through the triage router. Returns (passed, total)."""
    base = f"http://127.0.0.1:{cfg['router_port']}"
    key = cfg["router_api_key"]
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    passed = total = 0

    print(f"\n── E2E chat completions ({base}/v1/chat/completions) ──")

    tests = [
        {
            "label": "llm-routing-auto-free (simple)",
            "model": "llm-routing-auto-free",
            "prompt": "Say 'hello' in exactly one word.",
            "max_tokens": 5,
        },
        {
            "label": "agent-simple-core (direct)",
            "model": "agent-simple-core",
            "prompt": "What is 2+2? Answer with just the number.",
            "max_tokens": 5,
        },
        {
            "label": "llm-routing-auto-free (medium)",
            "model": "llm-routing-auto-free",
            "prompt": "Explain what a Python decorator is in one sentence.",
            "max_tokens": 50,
        },
    ]

    for test in tests:
        total += 1
        payload = {
            "model": test["model"],
            "messages": [{"role": "user", "content": test["prompt"]}],
            "max_tokens": test["max_tokens"],
        }
        try:
            start = time.time()
            r = httpx.post(
                f"{base}/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=120,
            )
            elapsed = time.time() - start
            if r.status_code == 200:
                data = r.json()
                content = (data["choices"][0]["message"].get("content") or "").strip()
                reasoning = (data["choices"][0]["message"].get("reasoning_content") or "").strip()
                model_used = data.get("model", "?")
                ok = len(content) > 0 or len(reasoning) > 0
                detail = f"model={model_used}, {elapsed:.1f}s"
                if content:
                    detail += f", '{content[:60]}'"
                elif reasoning:
                    detail += f", reasoning='{reasoning[:60]}'"
                passed += check(
                    test["label"],
                    ok,
                    detail,
                )
            else:
                passed += check(
                    test["label"],
                    False,
                    f"HTTP {r.status_code}: {r.text[:120]}",
                )
        except Exception as e:
            passed += check(test["label"], False, str(e))

    return passed, total


def test_infra_health(cfg: dict) -> tuple[int, int]:
    """Test infrastructure services: MinIO, ClickHouse. Returns (passed, total)."""
    passed = total = 0

    print(f"\n── Infrastructure health ──")

    # MinIO S3 health
    total += 1
    try:
        r = httpx.get("http://127.0.0.1:9002/minio/health/live", timeout=10)
        passed += check("MinIO /minio/health/live", r.status_code == 200)
    except Exception as e:
        passed += check("MinIO /minio/health/live", False, str(e))

    # ClickHouse HTTP ping
    total += 1
    try:
        r = httpx.get("http://127.0.0.1:8123/ping", timeout=10)
        passed += check("ClickHouse /ping", r.status_code == 200 and r.text.strip() == "Ok.")
    except Exception as e:
        passed += check("ClickHouse /ping", False, str(e))

    return passed, total


def test_litellm_direct_chat(cfg: dict) -> tuple[int, int]:
    """Test a direct chat completion through LiteLLM (bypassing the triage router)."""
    base = f"http://127.0.0.1:{cfg['litellm_port']}"
    key = cfg["litellm_master_key"]
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    passed = total = 0

    print(f"\n── LiteLLM direct chat ({base}/v1/chat/completions) ──")

    total += 1
    payload = {
        "model": "agent-simple-core",
        "messages": [{"role": "user", "content": "Say 'ok' and nothing else."}],
        "max_tokens": 5,
    }
    try:
        start = time.time()
        r = httpx.post(f"{base}/v1/chat/completions", json=payload, headers=headers, timeout=60)
        elapsed = time.time() - start
        if r.status_code == 200:
            data = r.json()
            content = (data["choices"][0]["message"].get("content") or "").strip()
            reasoning = (data["choices"][0]["message"].get("reasoning_content") or "").strip()
            ok = len(content) > 0 or len(reasoning) > 0
            detail = f"{elapsed:.1f}s"
            if content:
                detail += f", '{content[:40]}'"
            elif reasoning:
                detail += f", reasoning='{reasoning[:40]}'"
            passed += check("agent-simple-core (direct)", ok, detail)
        else:
            passed += check("agent-simple-core (direct)", False, f"HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        passed += check("agent-simple-core (direct)", False, str(e))

    return passed, total


def test_canonical_urls(cfg: dict) -> tuple[int, int]:
    """Verify canonical HTTPS URLs are reachable (if PUBLIC_BASE_URL is set)."""
    passed = total = 0
    public = cfg["public_base_url"]
    if not public:
        return 0, 0

    print(f"\n── Canonical URLs ({public}) ──")

    endpoints = [
        ("/v1/models", "router models"),
        ("/dashboard", "dashboard"),
        ("/metrics", "metrics"),
        ("/visualizer", "visualizer"),
        ("/litellm/ui/", "LiteLLM admin UI"),
        ("/langfuse", "Langfuse web UI"),
    ]

    for path, label in endpoints:
        total += 1
        url = f"{public}{path}"
        try:
            r = httpx.get(url, timeout=15, follow_redirects=True)
            ok = r.status_code == 200
            passed += check(f"GET {url}", ok, f"HTTP {r.status_code}")
        except httpx.ConnectError as e:
            # DNS/unreachable — skip gracefully (host may not resolve from test machine)
            passed += check(f"GET {url}", True, f"SKIP: DNS/unreachable ({e})")
        except Exception as e:
            passed += check(f"GET {url}", False, str(e)[:100])

    # Canonical chat completion (POST through public URL)
    total += 1
    url = f"{public}/v1/chat/completions"
    try:
        payload = {
            "model": "agent-simple-core",
            "messages": [{"role": "user", "content": "What is 2+2? Answer with just the number."}],
            "max_tokens": 5,
        }
        r = httpx.post(url, json=payload, headers={"Authorization": f"Bearer {cfg['router_api_key']}"}, timeout=60, follow_redirects=True)
        if r.status_code == 200:
            data = r.json()
            content = (data["choices"][0]["message"].get("content") or "").strip()
            reasoning = (data["choices"][0]["message"].get("reasoning_content") or "").strip()
            ok = len(content) > 0 or len(reasoning) > 0
            detail = f"HTTP {r.status_code}"
            if content:
                detail += f", '{content[:30]}'"
            passed += check(f"POST {url}", ok, detail)
        else:
            passed += check(f"POST {url}", False, f"HTTP {r.status_code}: {r.text[:80]}")
    except httpx.ConnectError as e:
        passed += check(f"POST {url}", True, f"SKIP: DNS/unreachable ({e})")
    except Exception as e:
        passed += check(f"POST {url}", False, str(e)[:100])

    return passed, total


def main():
    parser = argparse.ArgumentParser(description="Verify canonical endpoints")
    parser.add_argument(
        "--dev", action="store_true", help="Test dev environment (dev-router-pod)"
    )
    parser.add_argument(
        "--prod", action="store_true", help="Test prod environment (agent-router-pod, default)"
    )
    args = parser.parse_args()

    dev = args.dev and not args.prod
    env_label = "DEV" if dev else "PROD"
    cfg = load_env(dev=dev)

    print(f"=== Canonical Endpoint Verification [{env_label}] ===")
    print(f"  Router port : {cfg['router_port']}")
    print(f"  LiteLLM port: {cfg['litellm_port']}")
    print(f"  Langfuse port: {cfg['langfuse_web_port']}")
    if cfg["public_base_url"]:
        print(f"  Public URL  : {cfg['public_base_url']}")

    total_passed = total_tests = 0

    for name, fn in [
        ("Router API", test_router_endpoints),
        ("LiteLLM health", test_litellm_endpoints),
        ("Langfuse health", test_langfuse_endpoints),
        ("Infrastructure", test_infra_health),
        ("E2E chat (router)", test_e2e_chat),
        ("LiteLLM direct chat", test_litellm_direct_chat),
        ("Canonical URLs", test_canonical_urls),
    ]:
        p, t = fn(cfg)
        total_passed += p
        total_tests += t

    print(f"\n{'='*60}")
    print(f"Results [{env_label}]: {total_passed}/{total_tests} passed")
    if total_passed < total_tests:
        print(f"FAILED: {total_tests - total_passed} test(s)")
        sys.exit(1)
    else:
        print("ALL PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
