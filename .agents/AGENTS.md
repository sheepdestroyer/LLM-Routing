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
- **Notebook Name:** `LLM-Routing-KB`
- **Notebook ID:** `llm-triage-gateway`
- **Notebook URL:** [LLM-Routing-KB](https://notebooklm.google.com/notebook/826cbd87-7969-4b0e-a38e-5517b5ab7d28)

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

## Production Deployment Checklist

Note: Throughout this checklist, the production host SSH alias is represented by `<prod-host>` (e.g., `boy`), the deployer home path is represented by `<prod-home>` (e.g., `/mnt/DATA/boy`), and the domain is represented by `<prod-domain>` (e.g., `vendeuvre.lan`).

### One-Time Host Prerequisites
- `net.ipv4.ip_unprivileged_port_start=80` persisted in `/etc/sysctl.d/99-unprivileged-ports.conf`
- Host firewall ports `80/tcp` and `443/tcp` opened in `firewalld` (e.g. `sudo firewall-cmd --zone=public --add-port=80/tcp --permanent && sudo firewall-cmd --zone=public --add-port=443/tcp --permanent && sudo firewall-cmd --reload`)
- SSH host alias configured in `~/.ssh/config` — use `ssh <prod-host>` / `rsync ... <prod-host>:` throughout
- Required mount directories created under `<prod-home>`:
  - `<prod-home>/.gemini/`
  - `<prod-home>/.local/bin/agy` (copy of the `agy` binary)
  - `<prod-home>/.local/share/goose/`
  - `<prod-home>/.local/share/keyrings/`
- HAProxy SSL cert: `<prod-home>/haproxy/certs/<prod-domain>.pem`
- HAProxy config: `<prod-home>/haproxy/haproxy.cfg`

### Production Deployment (Gitless Minimal Deploy using GHCR Releases)
Production host does not require a full `git` repository checkout or local container build toolchain. Production pulls pre-built, tested release container images from GHCR (`ghcr.io/sheepdestroyer/llm-routing:<VERSION>`) and consumes the lightweight runtime deployment bundle.

```bash
# 1. Download minimal deployment bundle for target release (e.g. v0.1.25 or latest)
mkdir -p <prod-home>/LLM-Routing
curl -sSL https://github.com/sheepdestroyer/LLM-Routing/releases/download/v0.1.25/llm-routing-deploy.tar.gz | tar -xz -C <prod-home>/LLM-Routing

# 2. Deploy stack pulling GHCR container image (automatically triggers pre-deploy DB backup & restarts podman containers)
cd <prod-home>/LLM-Routing && ./start-stack.sh --pull

# 3. Ensure production HAProxy is running
podman rm -f production-haproxy || true
podman run -d --name production-haproxy --restart always --net host \
  -v <prod-home>/haproxy/haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro \
  -v <prod-home>/haproxy/certs:/usr/local/etc/haproxy/certs:ro \
  docker.io/library/haproxy:alpine

# 4. Start (or restart) the host-side agy daemon
pkill -f host_agy_daemon.py || true
nohup python3 <prod-home>/LLM-Routing/scripts/host_agy_daemon.py >/tmp/agy-daemon.log 2>&1 </dev/null &

# 5. Verify end-to-end dashboard health
# NOTE: -k is intentional — the HAProxy cert is self-signed (local CA).
curl -k -s --resolve <prod-domain>:443:127.0.0.1 https://<prod-domain>/llm-routing/dashboard | head -5
```

### Deployment Guidelines & Data Integrity
- **Non-Destructive Deployments**: Never execute `rm -rf <prod-home>/LLM-Routing` during production updates. Unpack release archives directly or use `start-stack.sh --pull` to preserve persistent volume data in `<prod-home>/LLM-Routing/data` and database backups in `<prod-home>/LLM-Routing/backups/`.
- **Pre-Deploy Backups**: `start-stack.sh` automatically runs `./scripts/backup.sh` before modifying any container or configuration. Backups are stored in `LLM-Routing/backups/` and retained for 14 days.
- **Database Restoration**: If a volume reset is ever required, restore database dumps using:
  - `podman exec -i prod-router-pod-postgres-db pg_restore -U postgres -d postgres --clean --if-exists < backups/postgres_db_<TIMESTAMP>.dump`
  - `podman exec -i prod-router-pod-postgres-db pg_restore -U postgres -d langfuse --clean --if-exists < backups/langfuse_db_<TIMESTAMP>.dump`
- The `agy-daemon.service` systemd unit cannot be reloaded via `systemctl --user` from
  the agent terminal (DBus is not connected). Start the daemon manually with `nohup` as
  shown above, or instruct the user to run it in their own session.
- **Sudo Password Precaution**: Always preserve exact bytes (including trailing spaces or newlines) when reading `~/.sudo_password` (e.g. `'your_password_here   '`). Stripping whitespace will cause authentication to fail.
- `start-stack.sh --pull` pulls pre-built release container images from GHCR without building locally.
  Use `--full-rebuild` only in local development (`DEV_ENV_FILE=.env.dev`).
- **GitHub CLI Authentication**: If running `gh` commands fails with a 401 error, ensure that `GITHUB_TOKEN` is exported (e.g., mapped from `GITHUB_MCP_PAT` in `~/.bashrc` via `export GITHUB_TOKEN="$GITHUB_MCP_PAT"`).

## GitHub API & Operations Policy
When interacting with the GitHub API or performing repository/PR metadata operations:
1. **Prefer `gh` CLI**: Always prefer using the GitHub CLI (`gh`) instead of executing raw `curl` commands.
2. **REST API Fallback via `gh api`**: If standard `gh` commands (like `gh pr view`) fail due to missing GraphQL token scopes (e.g., `read:org`), use `gh api` to run REST queries against the endpoint (e.g., `gh api repos/{owner}/{repo}/pulls/{pr_number}/reviews`) as it does not require GraphQL scopes.
