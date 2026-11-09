# LiteLLM salt key

LiteLLM encrypts every value it stores in its database (model `litellm_params`, `LiteLLM_CredentialsTable`, `LiteLLM_Config` rows, MCP and SSO secrets) with `LITELLM_SALT_KEY`, or with `LITELLM_MASTER_KEY` when no salt key is set. A salt key cannot be changed in place, and a master-key change without a salt key makes every stored value unreadable. With a salt key, the master key is an auth credential only and can be rotated freely.

## Where it lives

| Variable | Source of truth | Reaches LiteLLM as | Never in |
| :--- | :--- | :--- | :--- |
| `LITELLM_SALT_KEY` | `.env` | Podman secret `<namespace>-litellm-salt-key`, `Secret=...,type=env,target=LITELLM_SALT_KEY` in the LiteLLM unit | `effective.env` (the router reads it too), `Environment=` lines, output |
| `LITELLM_MIGRATE_FROM_MASTER_KEY` | `.env`, one boot only | Podman secret `<namespace>-litellm-migrate-from`, `Secret=...,type=env,target=LITELLM_MIGRATE_FROM_MASTER_KEY` | the same |

`start-stack.sh` runs `scripts/litellm_secrets.py apply` before the pre-deploy backup and before anything is stopped. It refuses a state that would leave LiteLLM unable to read what it stored (below), then writes the secret of the variable that is set, with the value on stdin. The renderer keeps a `Secret=` line only while its variable is set. After rendering, `litellm_secrets.py prune` removes the migrate-from secret once `LITELLM_MIGRATE_FROM_MASTER_KEY` is unset. `--stop` skips the check.

A stack whose `postgres-data` directory is empty, and that has no salt-key secret yet, gets a generated salt key (64 hex characters) in `.env` on its first start. An existing database never gets one: its values may be encrypted with the master key.

## Refused states

`apply` exits 1, and `start-stack.sh` stops with the running stack unchanged, when:

- `LITELLM_SALT_KEY` and `LITELLM_MIGRATE_FROM_MASTER_KEY` are both set (LiteLLM migrates nothing while a salt key is set);
- a migration boot's `LITELLM_MASTER_KEY` equals `LITELLM_MIGRATE_FROM_MASTER_KEY`, or a drop-in of the LiteLLM unit still passes `LITELLM_SALT_KEY`;
- `LITELLM_SALT_KEY` equals `LITELLM_MASTER_KEY`;
- `LITELLM_SALT_KEY` is unset while the salt-key secret exists;
- `LITELLM_SALT_KEY` differs from the existing secret, except in the second boot of a migration (below). To put an older key back on purpose, for example with a database restore, run `podman secret rm <namespace>-litellm-salt-key` first;
- a `*.conf` drop-in of the LiteLLM unit sets `LITELLM_SALT_KEY` in another way than from the salt-key secret.

## Changing the salt key: two boots

LiteLLM v1.108.0 has one supported way to move stored values to another key: its boot-time migration (`litellm/proxy/db/master_key_migration.py`). With `LITELLM_MIGRATE_FROM_MASTER_KEY` set, a new `LITELLM_MASTER_KEY` and no `LITELLM_SALT_KEY`, the boot re-encrypts every value that decrypts under the old key with the master key, before it serves.

Take a database backup first (`scripts/backup.sh`), and keep it with the old salt key until the change is verified.

1. **Migration boot.** In `.env`: remove `LITELLM_SALT_KEY`, set `LITELLM_MIGRATE_FROM_MASTER_KEY` to the current salt key and `LITELLM_MASTER_KEY` to a new key K1. Move any salt-key drop-in out of the LiteLLM unit's `.container.d/` directory (a name not ending in `.conf` is enough). Run `./start-stack.sh --replace`. LiteLLM logs `Re-encrypting N stored value(s) ...`, then `Done re-encrypting N stored value(s) with the new master key`.
2. **Second boot.** In `.env`: `LITELLM_SALT_KEY` = K1, `LITELLM_MASTER_KEY` = another key (the previous master key may come back), remove `LITELLM_MIGRATE_FROM_MASTER_KEY`. Run `./start-stack.sh --replace`. `apply` replaces the salt-key secret only when the migrate-from secret exists, the running LiteLLM logged in its current invocation (`_SYSTEMD_INVOCATION_ID`) that the migration finished (`Done re-encrypting ...`, or `... nothing in the database is left to migrate ...` from a later start), and K1 is the master key of the rendered (running) unit. The migrate-from secret is then removed.

Check afterwards: no `Error decrypting` line in the LiteLLM journal, every DB deployment listed by `/model/info`, one call through a stored credential.

Rollback before the second boot: restore the `.env` backup and `./start-stack.sh --replace` only if the migration boot has not run; once it has, restore the database backup as well. After the second boot: restore the database backup and the `.env` backup, `podman secret rm <namespace>-litellm-salt-key`, then `./start-stack.sh --replace`.

## The hand-made drop-in of 2026-11-08

Before v0.1.96 the repository did not pass the salt key, so the credential rotation of 2026-11-08 added `~/.config/containers/systemd/<namespace>/<namespace>-litellm.container.d/50-litellm-salt-key.conf` with `Secret=<namespace>-litellm-salt-key,type=env,target=LITELLM_SALT_KEY`. It names the same secret as the unit now does: Quadlet emits the flag twice and Podman accepts the duplicate, so both may coexist, and `apply` reports the drop-in as redundant. Keep it while a rollback to v0.1.95 or older is possible (those releases do not render the line), then delete it and run `systemctl --user daemon-reload`.
