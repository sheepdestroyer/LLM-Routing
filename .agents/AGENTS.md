# Agent Guidelines & Rules

## NotebookLM Knowledge Base Reference
When working on this project, always refer to the dedicated **NotebookLM Companion Notebook** for queries regarding:
- System Architecture & Topology
- LiteLLM configuration, cascades, and custom fallbacks
- agy proxy configurations and keyring authentication
- Ollama routing, rate limits, and custom cooldown implementations
- Langfuse v3 observability, telemetry pipelines, ClickHouse, and Minio integration
- Local model benchmark metrics and `llama-server` configurations

### Notebook Details
- **Notebook Name:** `TriageGate-Architect-KB`
- **Notebook ID:** `llm-triage-gateway`
- **Notebook URL:** [TriageGate-Architect-KB](https://notebooklm.google.com/notebook/826cbd87-7969-4b0e-a38e-5517b5ab7d28)

### How to Query
Use the `notebooklm` MCP tools to search or ask questions about this codebase and stack:
- Run `notebook_ask` with `notebook_id: "llm-triage-gateway"` to ground your reasoning or implementation plans.
- If you need session continuation, remember to reuse the `session_id` returned by previous queries.

## Git Rebase & Conflict Resolution Policy
To prevent directory reorganization regressions, outdated file restorations, or security credential overrides during merge conflict resolution, all automated agents must strictly follow these rules:

1. **Rebase Over Merge**: Always fetch and rebase the topic/feature branch onto the latest `master` base branch (using `git rebase origin/master`) instead of performing `git merge`.
2. **Directory Rename Safety**: If Git reports conflicts related to moved directories or files, do not manually stage deletions of tracked files from moved directories (e.g., under the old `tests/` or `scripts/` paths) or re-create files at the root level. Resolve conflicts by directing all changes and file operations to the newly refactored paths.
3. **Verify Security Credentials**: Never accept resolutions that overwrite configuration files (`pod.yaml`, `start-stack.sh`) with hardcoded default passwords. Ensure placeholder-based configurations are preserved.
4. **Enforce Test Suite Count**: Run the full unit test suite (`pytest`) after conflict resolution. Verify that the total number of passing tests is equal to or greater than before the resolution.
