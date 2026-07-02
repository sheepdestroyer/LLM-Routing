# Git Merges & Regressions Audit Report

## Problem Overview
During the merging of concurrent pull requests, automated conflict-resolution bots (`jules[bot]`) silently discarded or reverted several code blocks and logic optimizations. 

To ensure complete stability, we performed a systematic, automated three-dot diffing audit (`git diff CURRENT_BRANCH...TARGET_BRANCH`) comparing our current workspace against every active and merged remote branch in the repository.

---

## Branch-by-Branch Audit Checklist

| Branch Name / PR | Status | Notes / Discrepancies Checked |
| :--- | :---: | :--- |
| `origin/cleanup/remove-unused-get-live-gemini-oauth-token` (PR #195) | **CLEAN** | All changes present in current codebase. |
| `origin/perf-optimize-gemini-oauth-token-7488735575783948165` (PR #158) | **CLEAN** | All changes present in current codebase. |
| `origin/refactor-record-tool-usage-17779903941173107649` (PR #157) | **CLEAN** | All changes present in current codebase. |
| `origin/chore/get-pr-status-tests-18330387033569416831` (PR #193) | **CLEAN** | All changes present in current codebase. |
| `origin/chore/perf-async-annotations-4499495305587811104` (PR #172) | **CLEAN** | All changes present in current codebase. |
| `origin/chore/add-gemini-oauth-tests-17524029845400706758` (PR #163) | **CLEAN** | All changes present in current codebase. |
| `origin/dependabot/docker/berriai/litellm-v1.90.2` (PR #192) | **CLEAN** | Diff showed `v1.90.2` LiteLLM image version bump, which is already applied. |
| `origin/security/fix-memory-mcp-category-injection-14795006529015952965` (PR #173) | **CLEAN** | All changes present in current codebase. |
| `origin/test/add-read-annotations-sync-tests-12043640228306609857` (PR #168) | **CLEAN** | All changes present in current codebase. |
| `origin/chore/test-parse-key-parameterized-14651890987031951450` (PR #169) | **CLEAN** | All changes present in current codebase. |
| `origin/perf/async-quota-exhausted-4722057800691922723` (PR #156) | **CLEAN** | All changes present in current codebase. |
| `origin/perf-benchmark-classifier-14678555128983342452` (PR #155) | **CLEAN** | All changes present in current codebase. |
| `origin/chore/add-test-redis-from-url-12746122485451637611` (PR #174) | **CLEAN** | All changes present in current codebase. |
| `origin/chore/test-is-memory-key-14668590644102848561` (PR #164) | **CLEAN** | All changes present in current codebase. |
| `origin/⚡/optimize-agy-log-read-1519383298987985129` (PR #170) | **CLEAN** | All changes present in current codebase. |
| `origin/chore/add-oauth-status-tests-15591368961301252072` (PR #166) | **CLEAN** | All changes present in current codebase. |
| `origin/chore/test-get-goose-sessions-16558526271462662412` (PR #165) | **CLEAN** | All changes present in current codebase. |
| `origin/fix-http-client-proliferation-11317864999360875839` | **CLEAN** | HTTP client unification is fully present in our workspace. |
| `origin/fix/test-record-tool-usage-3580129709200353766` | **CLEAN** | Root-level test files were reorganized to `/tests`, fully preserved. |
| `origin/fix/secrets-relocation` (PR #184) | **CLEAN** | Checked out placeholders and updated `start-stack.sh`/`pod.yaml` securely. |
| `origin/perf/offload-aa-scores-sync-16551127323385849872` (PR #98) | **RESTORED** | **Regression Found**: The asynchronous scores loading optimization was lost on master. Re-implemented and verified. |

---

## Detailed Regression Analysis & Fix: Offload AA Scores Loading

### The Regression
* **File:** `router/main.py`
* **Issue:** `_load_aa_scores()` (which does a blocking synchronous JSON disk read of `aa_scores.json`) was called directly at free-model/roster call sites, specifically `sync_adaptive_router_roster()` and `get_best_free_model()`, rather than inside `compute_free_model_score()`. The fix was to move `_load_aa_scores()` behind `await asyncio.to_thread(...)` and remove the direct synchronous call from the scoring path.
* **Merged Fix Lost:** The merged PR #98 had offloaded this operation to a background thread pool using `await asyncio.to_thread(_load_aa_scores)` outside the loops in `sync_adaptive_router_roster` and `get_best_free_model`, but this change was completely discarded during automated conflict resolution.

### The Resolution
1. **`router/main.py`**:
   * Restored `await asyncio.to_thread(_load_aa_scores)` inside `sync_adaptive_router_roster()` and `get_best_free_model()` if the cache is not yet loaded.
   * Removed the blocking `_load_aa_scores()` call from inside `compute_free_model_score()`.
2. **`tests/test_compute_free_model_score.py`**:
   * Updated the test cases to explicitly call `router_main._load_aa_scores()` within the test setups to align with the asynchronous offloading.

---

## Verification Status
* **Test Suite:** Ran `PYTHONPATH=router:. pytest`
* **Result:** **181 tests passed** successfully (100% success rate).
