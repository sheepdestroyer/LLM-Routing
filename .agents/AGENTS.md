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
