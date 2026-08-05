import time
import pytest
from router import main as router_main


@pytest.fixture(autouse=True)
def clear_triage_cache():
    router_main.triage_cache.clear()
    yield
    router_main.triage_cache.clear()


def test_triage_cache_cleanup_expired():
    """Verify cleanup_triage_cache removes expired items."""
    now = time.time()
    router_main.triage_cache["fresh_key"] = ("agent-simple-core", now)
    router_main.triage_cache["expired_key"] = (
        "agent-complex-core",
        now - router_main.CACHE_TTL_SECONDS - 100,
    )

    router_main.cleanup_triage_cache()

    assert "fresh_key" in router_main.triage_cache
    assert "expired_key" not in router_main.triage_cache


def test_triage_cache_cap_max_size():
    """Verify cleanup_triage_cache caps size by evicting oldest items."""
    now = time.time()
    # Add 15 items with ascending timestamps
    for i in range(15):
        router_main.triage_cache[f"prompt_{i}"] = ("agent-simple-core", now + i)

    # Cap to max_size 10
    router_main.cleanup_triage_cache(max_size=10)

    assert len(router_main.triage_cache) == 10
    # Oldest 5 (prompt_0 .. prompt_4) should be evicted
    for i in range(5):
        assert f"prompt_{i}" not in router_main.triage_cache
    for i in range(5, 15):
        assert f"prompt_{i}" in router_main.triage_cache


def test_triage_cache_max_constant():
    """Verify MAX_TRIAGE_CACHE_SIZE constant is set to 10,000."""
    assert router_main.MAX_TRIAGE_CACHE_SIZE == 10000
