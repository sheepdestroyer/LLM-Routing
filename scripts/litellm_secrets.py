#!/usr/bin/env python3
"""LiteLLM's at-rest encryption keys as Podman secrets, for start-stack.sh.

LiteLLM encrypts every value it stores in its database (model ``litellm_params``, credentials,
``LiteLLM_Config`` rows, MCP and SSO secrets) with ``LITELLM_SALT_KEY``, or with the master key when
no salt key is set. A salt key cannot be changed in place. The one supported change of the encryption
key is LiteLLM's boot-time migration (v1.108.0, ``litellm/proxy/db/master_key_migration.py``): with
``LITELLM_MIGRATE_FROM_MASTER_KEY`` set, a new ``LITELLM_MASTER_KEY`` and no ``LITELLM_SALT_KEY``,
the boot re-encrypts every value stored under the old key with the master key, before serving.
Replacing a salt key therefore takes two boots:

1. ``LITELLM_SALT_KEY`` unset, ``LITELLM_MIGRATE_FROM_MASTER_KEY`` = the salt key,
   ``LITELLM_MASTER_KEY`` = K1, a new key;
2. ``LITELLM_SALT_KEY`` = K1, ``LITELLM_MASTER_KEY`` = a key other than K1,
   ``LITELLM_MIGRATE_FROM_MASTER_KEY`` unset.

Both variables live in ``.env``. LiteLLM gets each one as a Podman secret (``type=env``), never through
``effective.env`` (read by the router too) or an ``Environment=`` line:

- ``<ns>-litellm-salt-key``: ``LITELLM_SALT_KEY``;
- ``<ns>-litellm-migrate-from``: ``LITELLM_MIGRATE_FROM_MASTER_KEY``, first migration boot only.

Actions:

- ``apply``, before start-stack.sh changes anything: refuse a state that would leave LiteLLM unable to
  read what it stored, then create or replace the secret of the variable that is set (value on stdin).
- ``prune``, after the units are rendered: remove the migrate-from secret once its variable is unset.

Refused (exit 1):

- both variables set: LiteLLM migrates nothing while a salt key is set;
- a migration boot whose master key is the key to migrate from, or with a drop-in that still passes
  ``LITELLM_SALT_KEY``;
- ``LITELLM_SALT_KEY`` equal to ``LITELLM_MASTER_KEY``;
- ``LITELLM_SALT_KEY`` unset while the salt-key secret exists: the stored values need that key;
- ``LITELLM_SALT_KEY`` different from the existing secret, except in the second migration boot. That
  boot is recognised by three facts: the migrate-from secret exists, the running LiteLLM logged in this
  invocation that the migration finished, and the new salt key is that LiteLLM's master key. To put an
  older key back on purpose, ``podman secret rm <ns>-litellm-salt-key`` first;
- a drop-in of the LiteLLM unit that sets ``LITELLM_SALT_KEY`` other than from the salt-key secret.

Output names variables, files and secrets, never a value. Python stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

SALT_VAR = "LITELLM_SALT_KEY"
MIGRATE_VAR = "LITELLM_MIGRATE_FROM_MASTER_KEY"
MASTER_VAR = "LITELLM_MASTER_KEY"
MANUAL = "manual LLM-Routing.md §9o"

# LiteLLM v1.108.0 describe_outcome(): the two outcomes after which no stored value is left under the
# key to migrate from (Migrated with remaining=0, NOTHING_ENCRYPTED_WITH_PREVIOUS_KEY).
MIGRATION_DONE = re.compile(
    r"Done re-encrypting \d+ stored value\(s\) with the new master key"
    r"|LITELLM_MIGRATE_FROM_MASTER_KEY is still set, but nothing in the database is left to migrate"
)
_DIRECTIVE = re.compile(r"^\s*([A-Za-z]+)\s*=\s*(.*?)\s*$")
# start-stack.sh renders Environment="KEY=value" with backslashes and double quotes escaped.
_MASTER_LINE = re.compile(r'^Environment="LITELLM_MASTER_KEY=(.*)"\s*$')
_INVOCATION_ID = re.compile(r"[0-9a-f]{32}")

Runner = Callable[[Sequence[str], str | None], subprocess.CompletedProcess[str]]


class Refused(Exception):
    """A state start-stack.sh must not deploy. The message never contains a value."""


def run(argv: Sequence[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command with its output captured, never printed; a value only ever goes in on stdin."""
    if stdin is None:
        return subprocess.run(list(argv), stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
    return subprocess.run(list(argv), input=stdin, capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class Stack:
    """The names and paths of one stack's LiteLLM unit."""

    namespace: str
    quadlet_dir: Path
    env_file: str

    @property
    def salt_name(self) -> str:
        return f"{self.namespace}-litellm-salt-key"

    @property
    def migrate_name(self) -> str:
        return f"{self.namespace}-litellm-migrate-from"

    @property
    def litellm_unit(self) -> str:
        return f"{self.namespace}-litellm.service"

    @property
    def litellm_container(self) -> Path:
        return self.quadlet_dir / f"{self.namespace}-litellm.container"

    @property
    def dropin_dir(self) -> Path:
        return self.quadlet_dir / f"{self.namespace}-litellm.container.d"


@dataclass
class Plan:
    """The secret to write (and the variable it holds) and the status lines to print."""

    create: str | None = None
    source: str | None = None
    notes: list[str] = field(default_factory=list)


def secret_value(name: str, runner: Runner) -> str | None:
    """The data of a Podman secret, None when there is no such secret."""
    proc = runner(["podman", "secret", "inspect", "--showsecret", name], None)
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)[0]["SecretData"]
    except (ValueError, LookupError, TypeError):
        data = None
    if not isinstance(data, str):
        raise Refused(f"cannot read the Podman secret {name}: podman secret inspect gave no SecretData")
    return data


def secret_exists(name: str, runner: Runner) -> bool:
    return runner(["podman", "secret", "exists", name], None).returncode == 0


def salt_dropins(stack: Stack) -> list[tuple[Path, bool]]:
    """Each *.conf drop-in of the LiteLLM unit that sets LITELLM_SALT_KEY, and whether it does so
    exactly from the salt-key secret (the 2026-11-08 drop-in: Secret=<ns>-litellm-salt-key,...)."""
    expected = f"{stack.salt_name},type=env,target={SALT_VAR}"
    found = []
    for path in sorted(stack.dropin_dir.glob("*.conf")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise Refused(f"cannot read the drop-in {path}: {exc.strerror}") from None
        directives = []
        for line in text.splitlines():
            match = _DIRECTIVE.match(line)
            if match and not line.lstrip().startswith(("#", ";")):
                directives.append(match.groups())
        salt = [(key, value) for key, value in directives if SALT_VAR in value]
        if salt:
            found.append((path, all(k == "Secret" and v.replace(" ", "") == expected for k, v in salt)))
    return found


def migration_finished(stack: Stack, runner: Runner) -> bool:
    """True when the running LiteLLM unit logged, in this invocation, that no stored value is left
    under the key to migrate from."""
    show = runner(
        ["systemctl", "--user", "show", "--property=InvocationID", "--value", "--no-pager", stack.litellm_unit],
        None,
    )
    invocation = show.stdout.strip() if show.returncode == 0 else ""
    if not _INVOCATION_ID.fullmatch(invocation):
        return False
    journal = runner(["journalctl", "--user", "--no-pager", "-o", "cat", f"_SYSTEMD_INVOCATION_ID={invocation}"], None)
    return journal.returncode == 0 and MIGRATION_DONE.search(journal.stdout) is not None


def rendered_master_key(stack: Stack) -> str | None:
    """The LITELLM_MASTER_KEY of the rendered LiteLLM unit (the running boot's), None when absent."""
    try:
        lines = stack.litellm_container.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    value = None
    for line in lines:
        match = _MASTER_LINE.match(line)
        if match:
            value = re.sub(r"\\(.)", r"\1", match.group(1))
    return value


def _second_migration_boot(salt: str, stack: Stack, runner: Runner) -> str | None:
    """None when this is the second migration boot, else why it is not."""
    if not secret_exists(stack.migrate_name, runner):
        return f"no migration boot ran (there is no Podman secret {stack.migrate_name})"
    if not migration_finished(stack, runner):
        return (
            f"the running {stack.litellm_unit} has not logged that the migration finished; "
            f"let the migration boot complete, or start it again"
        )
    if rendered_master_key(stack) != salt:
        return f"{SALT_VAR} is not the {MASTER_VAR} of the migration boot ({stack.litellm_container})"
    return None


def plan(env: Mapping[str, str], stack: Stack, runner: Runner = run) -> Plan:
    """What apply writes, or Refused. See the module docstring for the rules."""
    salt = env.get(SALT_VAR) or ""
    migrate = env.get(MIGRATE_VAR) or ""
    master = env.get(MASTER_VAR) or ""
    where = stack.env_file
    dropins = salt_dropins(stack)
    result = Plan()
    if salt and migrate:
        raise Refused(
            f"{SALT_VAR} and {MIGRATE_VAR} are both set in {where}: LiteLLM migrates nothing while a salt key is "
            f"set. Remove {SALT_VAR} for the migration boot, and {MIGRATE_VAR} for the boot after it ({MANUAL})"
        )
    if migrate:
        if migrate == master:
            raise Refused(
                f"{MIGRATE_VAR} equals {MASTER_VAR}: the migration boot needs a new master key, the key the "
                f"stored values are re-encrypted with ({MANUAL})"
            )
        if dropins:
            raise Refused(
                f"{dropins[0][0]} passes {SALT_VAR}, so LiteLLM would skip the migration: move it out of "
                f"{stack.dropin_dir} for the migration boot ({MANUAL})"
            )
        result.create, result.source = stack.migrate_name, MIGRATE_VAR
        result.notes.append(
            f"ℹ️  Migration boot: LiteLLM re-encrypts the values stored under {MIGRATE_VAR} with {MASTER_VAR}. "
            f"Then set {SALT_VAR} to that {MASTER_VAR}, give {MASTER_VAR} another value and remove {MIGRATE_VAR}"
        )
        return result
    current = secret_value(stack.salt_name, runner)
    if not salt:
        if current is not None:
            raise Refused(
                f"{SALT_VAR} is not set in {where}, but the Podman secret {stack.salt_name} exists: LiteLLM's "
                f"stored values are encrypted with it. Put {SALT_VAR} back into {where} ({MANUAL})"
            )
        result.notes.append(
            f"⚠️  {SALT_VAR} is not set: LiteLLM encrypts its stored values with {MASTER_VAR}, so changing the "
            f"master key makes them unreadable ({MANUAL})"
        )
        return result
    if salt == master:
        raise Refused(
            f"{SALT_VAR} equals {MASTER_VAR}: the salt key must never be an auth credential. Give "
            f"{MASTER_VAR} a new value ({MANUAL})"
        )
    for path, from_secret in dropins:
        if not from_secret:
            raise Refused(
                f"{path} sets {SALT_VAR} other than from the Podman secret {stack.salt_name}: remove that "
                f"line ({MANUAL})"
            )
        result.notes.append(
            f"ℹ️  {path} is redundant: the LiteLLM unit passes {stack.salt_name} itself (kept so that a "
            f"rollback to v0.1.95 or older keeps {SALT_VAR})"
        )
    if current is not None and current != salt:
        reason = _second_migration_boot(salt, stack, runner)
        if reason is not None:
            raise Refused(
                f"{SALT_VAR} in {where} differs from the Podman secret {stack.salt_name}, the key LiteLLM's "
                f"stored values are encrypted with, and {reason}. Replacing it would make them unreadable: "
                f"change it with the two-boot migration ({MANUAL}). To put an older key back on purpose, "
                f"run podman secret rm {stack.salt_name} first"
            )
        result.notes.append(
            f"ℹ️  Second migration boot: {SALT_VAR} is the master key the stored values were re-encrypted "
            f"with, so {stack.salt_name} is replaced"
        )
    result.create, result.source = stack.salt_name, SALT_VAR
    return result


def apply(env: Mapping[str, str], stack: Stack, runner: Runner = run) -> list[str]:
    """Check the state, then write the secret of the variable that is set (value on stdin)."""
    result = plan(env, stack, runner)
    lines = list(result.notes)
    if result.create is not None and result.source is not None:
        proc = runner(["podman", "secret", "create", "--replace", result.create, "-"], env[result.source])
        if proc.returncode != 0:
            raise Refused(f"podman secret create {result.create} failed (exit {proc.returncode})")
        lines.append(f"✓ Podman secret {result.create} written from {result.source}")
    return lines


def prune(env: Mapping[str, str], stack: Stack, runner: Runner = run) -> list[str]:
    """Remove the migrate-from secret once LITELLM_MIGRATE_FROM_MASTER_KEY is no longer set."""
    if env.get(MIGRATE_VAR) or not secret_exists(stack.migrate_name, runner):
        return []
    proc = runner(["podman", "secret", "rm", stack.migrate_name], None)
    if proc.returncode != 0:
        raise Refused(f"podman secret rm {stack.migrate_name} failed (exit {proc.returncode})")
    return [f"✓ Podman secret {stack.migrate_name} removed ({MIGRATE_VAR} is no longer set)"]


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None, runner: Runner = run) -> int:
    parser = argparse.ArgumentParser(
        prog="litellm_secrets.py",
        description="LiteLLM's salt key (and migrate-from key) as Podman secrets; run by start-stack.sh.",
    )
    parser.add_argument("action", choices=("apply", "prune"))
    parser.add_argument("--namespace", required=True, help="Quadlet namespace, e.g. llm-routing-prod")
    parser.add_argument("--quadlet-dir", type=Path, required=True, help="the namespace's rendered unit directory")
    parser.add_argument("--env-file", default=".env", help="named in messages (values come from the environment)")
    args = parser.parse_args(argv)
    stack = Stack(args.namespace, args.quadlet_dir, args.env_file)
    action = apply if args.action == "apply" else prune
    try:
        lines = action(os.environ if environ is None else environ, stack, runner)
    except Refused as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    for line in lines:
        print(f"   {line}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
