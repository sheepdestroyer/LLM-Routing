import pytest
from playwright.async_api import Page, expect


@pytest.mark.anyio
async def test_visualizer_empty_state_and_layout(page: Page, base_url: str):
    """Verify that the visualizer loads with header stats, filter controls, and empty detail state."""
    await page.goto(f"{base_url}/visualizer")

    # Title check
    await expect(page).to_have_title("Classifier Dataset Visualizer")

    # Header and stats counters
    await expect(page.locator(".header h1")).to_contain_text("Dataset Visualizer")
    await expect(page.locator("#stat-total")).to_be_visible()
    await expect(page.locator("#stat-agree")).to_be_visible()
    await expect(page.locator("#stat-conflict")).to_be_visible()
    await expect(page.locator("#stat-reviewed")).to_be_visible()

    # Filter controls
    await expect(page.locator("#btn-all")).to_be_visible()
    await expect(page.locator("#btn-conflict")).to_be_visible()
    await expect(page.locator("#btn-agree")).to_be_visible()
    await expect(page.locator("#btn-unreviewed")).to_be_visible()

    # Empty state message
    await expect(page.locator(".empty-state h2")).to_contain_text("Select a prompt from the list")


@pytest.mark.anyio
async def test_visualizer_dataset_rendering_and_selection(page: Page, base_url: str):
    """Test mock dataset loading, list rendering, prompt selection, and side-by-side evaluation view."""
    mock_dataset = {
        "agreement": 50,
        "prompts": [
            {
                "prompt": "Explain what a binary search tree is in simple terms.",
                "llm_tier": "agent-simple-core",
                "clf_tier": "agent-simple-core",
            },
            {
                "prompt": "Write a distributed multi-agent consensus algorithm in Rust with raft leadership election.",
                "llm_tier": "agent-advanced-core",
                "clf_tier": "agent-medium-core",
            },
        ],
    }

    # Intercept data files
    await page.route("**/data/classified_dataset.json", lambda route: route.fulfill(
        status=200, content_type="application/json", json=mock_dataset
    ))
    await page.route("**/data/benchmark_results.json", lambda route: route.fulfill(
        status=200, content_type="application/json", json={}
    ))
    await page.route("**/data/annotations.json", lambda route: route.fulfill(
        status=200, content_type="application/json", json={}
    ))

    await page.goto(f"{base_url}/visualizer")

    # Check stats updated
    await expect(page.locator("#stat-total")).to_have_text("2")
    await expect(page.locator("#stat-agree")).to_have_text("1")
    await expect(page.locator("#stat-conflict")).to_have_text("1")

    # Check list items rendered
    list_items = page.locator(".list-item")
    await expect(list_items).to_have_count(2)

    # First item has AGREE tag, second has CONFLICT tag
    await expect(list_items.nth(0).locator(".tag.agree")).to_contain_text("AGREE")
    await expect(list_items.nth(1).locator(".tag.conflict")).to_contain_text("CONFLICT")

    # Select the second prompt (conflict)
    await list_items.nth(1).click()

    # Verify detail panel updates
    prompt_text = page.locator(".prompt-text")
    await expect(prompt_text).to_contain_text("distributed multi-agent consensus algorithm")

    # Verify side-by-side comparison cards
    await expect(page.locator(".evals")).to_be_visible()
    await expect(page.locator(".eval-card.mismatch").first).to_contain_text("agent-advanced-core")
    await expect(page.locator(".eval-card.mismatch").last).to_contain_text("agent-medium-core")

    # Verify human review tier buttons
    await expect(page.locator(".human-review")).to_be_visible()
    await expect(page.locator(".tier-buttons .tier-btn")).to_have_count(5)


@pytest.mark.anyio
async def test_visualizer_filters(page: Page, base_url: str):
    """Test filtering by Conflicts, Agreements, Unreviewed, and All."""
    mock_dataset = {
        "prompts": [
            {"prompt": "Simple prompt", "llm_tier": "agent-simple-core", "clf_tier": "agent-simple-core"},
            {"prompt": "Conflicting prompt", "llm_tier": "agent-advanced-core", "clf_tier": "agent-simple-core"},
        ]
    }

    await page.route("**/data/classified_dataset.json", lambda route: route.fulfill(
        status=200, content_type="application/json", json=mock_dataset
    ))
    await page.route("**/data/benchmark_results.json", lambda route: route.fulfill(status=200, json={}))
    await page.route("**/data/annotations.json", lambda route: route.fulfill(status=200, json={}))

    await page.goto(f"{base_url}/visualizer")

    # Initial state (All): 2 items
    await expect(page.locator(".list-item")).to_have_count(2)
    await expect(page.locator("#filter-count")).to_have_text("2")

    # Filter Conflicts
    await page.locator("#btn-conflict").click()
    await expect(page.locator(".list-item")).to_have_count(1)
    await expect(page.locator(".list-item .snippet")).to_contain_text("Conflicting prompt")

    # Filter Agreements
    await page.locator("#btn-agree").click()
    await expect(page.locator(".list-item")).to_have_count(1)
    await expect(page.locator(".list-item .snippet")).to_contain_text("Simple prompt")

    # Reset to All
    await page.locator("#btn-all").click()
    await expect(page.locator(".list-item")).to_have_count(2)


@pytest.mark.anyio
async def test_visualizer_human_annotation_interaction(page: Page, base_url: str):
    """Test human annotation selection, note addition, and saving."""
    mock_dataset = {
        "prompts": [
            {"prompt": "Explain asyncio in Python", "llm_tier": "agent-medium-core", "clf_tier": "agent-simple-core"},
        ]
    }

    save_requests = []

    async def handle_save(route):
        save_requests.append(route.request)
        await route.fulfill(status=200, json={"status": "ok"})

    await page.route("**/dashboard/save-annotations*", handle_save)
    await page.route("**/data/classified_dataset.json", lambda route: route.fulfill(status=200, json=mock_dataset))
    await page.route("**/data/benchmark_results.json", lambda route: route.fulfill(status=200, json={}))
    await page.route("**/data/annotations.json", lambda route: route.fulfill(status=200, json={}))

    await page.goto(f"{base_url}/visualizer")

    # Select prompt
    await page.locator(".list-item").first.click()

    # Click tier button for 'complex-core'
    complex_btn = page.locator(".tier-btn", has_text="complex-core")
    await complex_btn.click()

    # Enter review note
    note_input = page.locator("#note-input")
    await note_input.fill("Requires understanding of event loops and coroutines.")

    save_note_btn = page.locator(".annotation-bar button", has_text="Save Note")
    await save_note_btn.click()
    assert len(save_requests) >= 1

    # Verify reviewed badge in list
    await expect(page.locator(".tag.human")).to_contain_text("REVIEWED")
    await expect(page.locator("#stat-reviewed")).to_have_text("1")


@pytest.mark.anyio
async def test_visualizer_clear_annotation(page: Page, base_url: str):
    """Test that existing annotations can be cleared."""
    mock_dataset = {
        "prompts": [
            {"prompt": "Calculate Fibonacci in O(n)", "llm_tier": "agent-simple-core", "clf_tier": "agent-simple-core"},
        ]
    }

    await page.route("**/dashboard/save-annotations*", lambda route: route.fulfill(status=200, json={"status": "ok"}))
    await page.route("**/data/classified_dataset.json", lambda route: route.fulfill(status=200, json=mock_dataset))
    await page.route("**/data/benchmark_results.json", lambda route: route.fulfill(status=200, json={}))
    await page.route("**/data/annotations.json", lambda route: route.fulfill(status=200, json={}))

    await page.goto(f"{base_url}/visualizer")

    # Select prompt and annotate
    await page.locator(".list-item").first.click()
    await page.locator(".tier-btn", has_text="medium-core").click()
    await expect(page.locator(".tag.human")).to_contain_text("REVIEWED")

    # Click clear link
    clear_link = page.locator(".human-review a", has_text="clear")
    await expect(clear_link).to_be_visible()
    await clear_link.click()

    # Verify annotation removed
    await expect(page.locator(".tag.human")).to_have_count(0)
    await expect(page.locator("#stat-reviewed")).to_have_text("0")


@pytest.mark.anyio
async def test_visualizer_keyboard_navigation(page: Page, base_url: str):
    """Test that list items can be focused and selected via keyboard Enter key."""
    mock_dataset = {
        "prompts": [
            {"prompt": "First keyboard navigable prompt", "llm_tier": "agent-simple-core", "clf_tier": "agent-simple-core"},
            {"prompt": "Second keyboard navigable prompt", "llm_tier": "agent-medium-core", "clf_tier": "agent-medium-core"},
        ]
    }

    await page.route("**/data/classified_dataset.json", lambda route: route.fulfill(status=200, json=mock_dataset))
    await page.route("**/data/benchmark_results.json", lambda route: route.fulfill(status=200, json={}))
    await page.route("**/data/annotations.json", lambda route: route.fulfill(status=200, json={}))

    await page.goto(f"{base_url}/visualizer")

    # Focus the second list item and press Enter
    second_item = page.locator(".list-item").nth(1)
    await second_item.focus()
    await page.keyboard.press("Enter")

    # Verify detail view is rendered for the second prompt
    await expect(page.locator(".prompt-text")).to_contain_text("Second keyboard navigable prompt")
