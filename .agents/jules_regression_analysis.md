# Analysis of Bot Merge Conflict Regressions & Guardrails

This document breaks down the root causes of the regressions introduced by the automated agent (`jules[bot]`) during recent merges, compares Git resolution strategies, and outlines prompt modifications and guardrails to prevent future occurrences.

---

## 1. What Went Wrong?

The primary failure arose from a combination of **reorganization conflict logic** and **blind resolution choices**:

### A. The Directory Rename / Delete Conflict Mismatch
* **Context**: PR #181 reorganized the repository by moving files at the root (like `test_a2_verify.py` and `host_agy_daemon.py`) into nested directories (`tests/` and `scripts/`).
* **The Bot's Branch State**: The bot was working on older branches (`perf-optimize-gemini-oauth-token`, `refactor-record-tool-usage`) whose base commits predated the reorganization.
* **The Failure**: When merging `master` into these older feature branches, Git detected conflict types like `rename/delete` or `directory rename detection`. Because the bot resolved conflicts locally inside the old workspace structure, it staged the **deletion** of the new files inside `tests/` and `scripts/` and re-introduced older, obsolete root-level files.
* **Result**: Merges from these branches back to `master` cleanly deleted the subfolder-bound tests and re-added old copies at the root.

### B. Lack of Cross-Check Verification
* The bot resolved conflicts file-by-file without compiling the whole project or validating the full test suite (`pytest`) on the combined codebase.
* It accepted obsolete file versions wholesale (e.g., reverting the dynamic passwords in `pod.yaml` and `start-stack.sh` to hardcoded credentials) because it assumed conflict markers in one block did not impact security/logic blocks elsewhere.

---

## 2. Which Git Strategy Should Have Been Used?

Instead of merging `master` directly into older feature branches (which creates complex, multi-directional merge graphs), the following strategies are far safer for LLM agents:

### Option A: `git rebase master` (Recommended for Feature Branches)
Rather than a merge commit, the agent should rebase the feature branch onto the latest `master` commit:
```bash
git checkout feature-branch
git fetch origin
git rebase origin/master
```
* **Why it works**: Rebase replays each feature branch commit one-by-one on top of the reorganized `master` commit. 
* **Verify Relocated Files**: While Git's rename detection can assist with directory/file moves during rebase, it is not foolproof. The agent must manually verify relocated files (such as renamed test paths) after rebasing to ensure all changes landed in their correct new locations rather than being lost or orphaned.

### Option B: Merge with explicit Merge Drivers
If rebasing is not used, the agent must inspect renames before committing:
```bash
git diff --name-status origin/master...HEAD
```
This lists any deleted/added files to quickly verify that no folder moves were silently discarded.

---

## 3. Recommended Prompt Guardrails and System Rules

To prevent automated agents from causing directory and code regressions, the following rules should be appended to the agent's instructions (e.g., in `.agents/AGENTS.md` or system guidelines):

### Rule 1: Git Conflict Rebase Mandate
> [!IMPORTANT]
> When updating a feature branch with `master`/`main` changes, always prefer `git rebase` over `git merge`. If conflicts arise due to renamed directories, do not manually delete folders or re-add root counterparts. Ensure files are modified in their new paths.

### Rule 2: Complete Test Suite Verification
> [!WARNING]
> Never push conflict resolutions without running the full test suite.
> * If the workspace previously had $N$ passing tests, the resolved branch must have at least $N$ passing tests.
> * Confirm all files staged for deletion, addition, or rename are intentional by running `git diff --name-only origin/master...HEAD` and inspecting the content diffs of renamed or moved files.

### Rule 3: File Integrity & Verification Checklist
Add a post-conflict verification script step to the agent workflow:
```bash
# Verify no files were moved to root unexpectedly
git status --porcelain
```

---

## 4. Proposed Instructions / Prompts for Bot Agents

When tasking an agent with conflict resolution or merging, use a structured prompt like this:

```markdown
You are resolving merge conflicts for the branch [branch_name].

1. Run `git fetch origin` and `git rebase origin/master`.
2. If conflicts arise, inspect whether any files were renamed/moved in master. Apply your changes to the renamed files in their new locations, rather than re-creating them at their old locations.
3. Once rebased, run the entire test suite: `pytest`.
4. Run `git diff --name-only origin/master...HEAD` and inspect the full content diff for renamed, deleted, or restored files.
5. Only restore files from `master` after confirming the deletion was accidental; do not auto-checkout files that may have been intentionally renamed or removed.
```
