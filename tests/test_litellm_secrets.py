"""scripts/litellm_secrets.py: LiteLLM's salt key and migrate-from key as Podman secrets.

Unit tests drive plan/apply/prune with an in-memory podman/systemctl/journalctl. The end-to-end
tests run the real CLI against stub binaries on PATH, through both boots of a salt-key migration.
No test reaches the real Podman secret store, systemd or journal.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import litellm_secrets as ls

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "litellm_secrets.py"
NS = "llm-routing-prod"
SALT_SECRET = f"{NS}-litellm-salt-key"
MIGRATE_SECRET = f"{NS}-litellm-migrate-from"
OLD_SALT = "sk-litellm-old0salt0value0000000000000000"
MASTER = "sk-master-current-0123456789abcdef0123456789abcdef"
# A quote and a backslash, to prove the rendered unit's escaping is undone (not a real key format).
K1 = 'sk-new"salt\\value-test-only-not-a-real-credential'
INVOCATION = "0123456789abcdef0123456789abcdef"
DONE_LINE = (
    "2026-11-09 12:00:00 [INFO] [LiteLLM Proxy] proxy_server.py:1 - Done re-encrypting 97 stored value(s) "
    "with the new master key. You may now delete the LITELLM_MIGRATE_FROM_MASTER_KEY environment variable."
)
NOTHING_LEFT_LINE = (
    "LITELLM_MIGRATE_FROM_MASTER_KEY is still set, but nothing in the database is left to migrate from that "
    "key. You may now delete LITELLM_MIGRATE_FROM_MASTER_KEY."
)
SECRETS = (OLD_SALT, MASTER, K1)

# The drop-in the 2026-11-08 rotation installed by hand (holds a secret name, no value).
LEGACY_DROPIN = f"""# LITELLM_SALT_KEY for the prod LiteLLM gateway (added 2026-11-08, credential rotation).
#
# Why a drop-in: LLM-Routing v0.1.93 start-stack.sh does not pass LITELLM_SALT_KEY.
#   podman secret create --replace {SALT_SECRET} - < value
[Container]
Secret={SALT_SECRET},type=env,target=LITELLM_SALT_KEY
"""


def _done(argv, code=0, stdout=""):
    return subprocess.CompletedProcess(list(argv), code, stdout, "")


class FakeHost:
    """podman secrets in memory; systemctl reports `invocation`; journalctl prints `journal`."""

    def __init__(self, secrets=None, invocation="", journal="", fail=()):
        self.secrets = dict(secrets or {})
        self.invocation = invocation
        self.journal = journal
        self.fail = set(fail)
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, argv, stdin):
        argv = list(argv)
        self.calls.append((argv, stdin))
        if argv[:3] == ["podman", "secret", "inspect"]:
            name = argv[-1]
            if name not in self.secrets:
                return _done(argv, 125)
            if "inspect-garbage" in self.fail:
                return _done(argv, 0, "not json")
            return _done(argv, 0, json.dumps([{"Spec": {"Name": name}, "SecretData": self.secrets[name]}]))
        if argv[:3] == ["podman", "secret", "exists"]:
            return _done(argv, 0 if argv[3] in self.secrets else 1)
        if argv[:3] == ["podman", "secret", "create"]:
            if "create" in self.fail:
                return _done(argv, 125)
            assert argv[3] == "--replace" and argv[5] == "-"
            self.secrets[argv[4]] = stdin
            return _done(argv)
        if argv[:3] == ["podman", "secret", "rm"]:
            if "rm" in self.fail:
                return _done(argv, 125)
            del self.secrets[argv[3]]
            return _done(argv)
        if argv[0] == "systemctl":
            return _done(argv, 1 if "show" in self.fail else 0, self.invocation + "\n")
        if argv[0] == "journalctl":
            assert argv[-1] == f"_SYSTEMD_INVOCATION_ID={self.invocation}"
            return _done(argv, 1 if "journal" in self.fail else 0, self.journal)
        raise AssertionError(argv)


@pytest.fixture
def stack(tmp_path):
    quadlet_dir = tmp_path / "systemd" / NS
    quadlet_dir.mkdir(parents=True)
    return ls.Stack(NS, quadlet_dir, "/home/u/prod/LLM-Routing/.env")


def _dropin(stack, text, name="50-litellm-salt-key.conf"):
    stack.dropin_dir.mkdir(exist_ok=True)
    path = stack.dropin_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def _render_master(stack, master):
    """The rendered LiteLLM unit as start-stack.sh writes it (Environment= quoted and escaped)."""
    escaped = master.replace("\\", "\\\\").replace('"', '\\"')
    stack.litellm_container.write_text(
        f'[Container]\nEnvironment="LITELLM_LOG=INFO"\nEnvironment="LITELLM_MASTER_KEY={escaped}"\n',
        encoding="utf-8",
    )


def _no_values(lines):
    text = "\n".join(lines)
    for value in SECRETS:
        assert value not in text


def test_steady_state_rewrites_the_salt_secret_from_env(stack):
    host = FakeHost({SALT_SECRET: OLD_SALT})
    lines = ls.apply({"LITELLM_SALT_KEY": OLD_SALT, "LITELLM_MASTER_KEY": MASTER}, stack, host)
    assert lines == [f"✓ Podman secret {SALT_SECRET} written from LITELLM_SALT_KEY"]
    create = [c for c in host.calls if c[0][:3] == ["podman", "secret", "create"]]
    # The value goes in on stdin only, never in argv.
    assert create == [(["podman", "secret", "create", "--replace", SALT_SECRET, "-"], OLD_SALT)]
    assert all(OLD_SALT not in " ".join(argv) for argv, _ in host.calls)


def test_first_render_creates_the_salt_secret(stack):
    host = FakeHost()
    ls.apply({"LITELLM_SALT_KEY": OLD_SALT, "LITELLM_MASTER_KEY": MASTER}, stack, host)
    assert host.secrets == {SALT_SECRET: OLD_SALT}


def test_legacy_dropin_is_reported_redundant_and_kept(stack):
    path = _dropin(stack, LEGACY_DROPIN)
    host = FakeHost({SALT_SECRET: OLD_SALT})
    lines = ls.apply({"LITELLM_SALT_KEY": OLD_SALT, "LITELLM_MASTER_KEY": MASTER}, stack, host)
    assert lines[0].startswith(f"ℹ️  {path} is redundant: the LiteLLM unit passes {SALT_SECRET} itself")
    assert "rollback to v0.1.95 or older" in lines[0]
    assert path.read_text(encoding="utf-8") == LEGACY_DROPIN
    _no_values(lines)


@pytest.mark.parametrize(
    "directive",
    [
        "Environment=LITELLM_SALT_KEY=inline",
        'Environment="LITELLM_SALT_KEY=inline"',
        "Secret=some-other-secret,type=env,target=LITELLM_SALT_KEY",
    ],
)
def test_a_dropin_setting_the_salt_another_way_is_refused(stack, directive):
    path = _dropin(stack, f"[Container]\n{directive}\n", "60-extra.conf")
    host = FakeHost({SALT_SECRET: OLD_SALT})
    with pytest.raises(ls.Refused, match=f"{path} sets LITELLM_SALT_KEY other than from the Podman secret"):
        ls.apply({"LITELLM_SALT_KEY": OLD_SALT, "LITELLM_MASTER_KEY": MASTER}, stack, host)
    assert SALT_SECRET in host.secrets and host.secrets[SALT_SECRET] == OLD_SALT


def test_dropins_without_the_salt_and_other_files_are_ignored(stack):
    _dropin(stack, "[Container]\n# LITELLM_SALT_KEY is not set here\nEnvironment=FOO=bar\n", "10-other.conf")
    _dropin(stack, "[Container]\nSecret=x,type=env,target=LITELLM_SALT_KEY\n", "50-litellm-salt-key.conf.migrating")
    host = FakeHost({SALT_SECRET: OLD_SALT})
    lines = ls.apply({"LITELLM_SALT_KEY": OLD_SALT, "LITELLM_MASTER_KEY": MASTER}, stack, host)
    assert lines == [f"✓ Podman secret {SALT_SECRET} written from LITELLM_SALT_KEY"]


def test_an_unreadable_dropin_is_refused(stack):
    stack.dropin_dir.mkdir()
    (stack.dropin_dir / "50-broken.conf").mkdir()
    with pytest.raises(ls.Refused, match=r"cannot read the drop-in .*50-broken\.conf"):
        ls.apply({"LITELLM_SALT_KEY": OLD_SALT, "LITELLM_MASTER_KEY": MASTER}, stack, FakeHost())


def test_both_variables_set_is_refused(stack):
    env = {"LITELLM_SALT_KEY": OLD_SALT, "LITELLM_MIGRATE_FROM_MASTER_KEY": OLD_SALT, "LITELLM_MASTER_KEY": K1}
    host = FakeHost({SALT_SECRET: OLD_SALT})
    with pytest.raises(ls.Refused, match="LITELLM_SALT_KEY and LITELLM_MIGRATE_FROM_MASTER_KEY are both set"):
        ls.apply(env, stack, host)
    assert host.calls == []


def test_migration_boot_needs_a_new_master_key(stack):
    env = {"LITELLM_MIGRATE_FROM_MASTER_KEY": OLD_SALT, "LITELLM_MASTER_KEY": OLD_SALT}
    with pytest.raises(ls.Refused, match="equals LITELLM_MASTER_KEY: the migration boot needs a new master key"):
        ls.apply(env, stack, FakeHost())


def test_migration_boot_refuses_a_salt_dropin(stack):
    path = _dropin(stack, LEGACY_DROPIN)
    env = {"LITELLM_MIGRATE_FROM_MASTER_KEY": OLD_SALT, "LITELLM_MASTER_KEY": K1}
    with pytest.raises(ls.Refused, match=f"{path} passes LITELLM_SALT_KEY, so LiteLLM would skip the migration"):
        ls.apply(env, stack, FakeHost({SALT_SECRET: OLD_SALT}))


def test_migration_boot_writes_the_migrate_from_secret(stack):
    _dropin(stack, LEGACY_DROPIN, "50-litellm-salt-key.conf.migrating")
    host = FakeHost({SALT_SECRET: OLD_SALT})
    env = {"LITELLM_MIGRATE_FROM_MASTER_KEY": OLD_SALT, "LITELLM_MASTER_KEY": K1}
    lines = ls.apply(env, stack, host)
    assert lines[0].startswith("ℹ️  Migration boot: LiteLLM re-encrypts the values stored under")
    assert lines[1] == f"✓ Podman secret {MIGRATE_SECRET} written from LITELLM_MIGRATE_FROM_MASTER_KEY"
    # The salt-key secret is left alone: a rollback needs it.
    assert host.secrets == {SALT_SECRET: OLD_SALT, MIGRATE_SECRET: OLD_SALT}
    _no_values(lines)


def test_salt_removed_from_env_while_its_secret_exists_is_refused(stack):
    with pytest.raises(ls.Refused, match=f"LITELLM_SALT_KEY is not set in .*, but the Podman secret {SALT_SECRET}"):
        ls.apply({"LITELLM_MASTER_KEY": MASTER}, stack, FakeHost({SALT_SECRET: OLD_SALT}))


def test_no_salt_at_all_only_warns(stack):
    host = FakeHost()
    lines = ls.apply({"LITELLM_MASTER_KEY": MASTER, "LITELLM_SALT_KEY": ""}, stack, host)
    assert len(lines) == 1 and lines[0].startswith("⚠️  LITELLM_SALT_KEY is not set: LiteLLM encrypts")
    assert host.secrets == {}


def test_salt_equal_to_master_is_refused(stack):
    with pytest.raises(ls.Refused, match="LITELLM_SALT_KEY equals LITELLM_MASTER_KEY"):
        ls.apply({"LITELLM_SALT_KEY": MASTER, "LITELLM_MASTER_KEY": MASTER}, stack, FakeHost())


def test_changed_salt_without_a_migration_is_refused(stack):
    host = FakeHost({SALT_SECRET: OLD_SALT})
    with pytest.raises(ls.Refused) as info:
        ls.apply({"LITELLM_SALT_KEY": K1, "LITELLM_MASTER_KEY": MASTER}, stack, host)
    message = str(info.value)
    assert f"differs from the Podman secret {SALT_SECRET}" in message
    assert f"no migration boot ran (there is no Podman secret {MIGRATE_SECRET})" in message
    assert f"podman secret rm {SALT_SECRET} first" in message
    _no_values([message])
    assert host.secrets == {SALT_SECRET: OLD_SALT}


@pytest.mark.parametrize(
    ("invocation", "journal", "fail"),
    [
        ("", DONE_LINE, ()),  # LiteLLM not running: no invocation to read
        ("not-an-id", DONE_LINE, ()),
        (INVOCATION, DONE_LINE, ("show",)),
        (INVOCATION, "Re-encrypting 97 stored value(s) from the ... key", ()),  # started, not finished
        (INVOCATION, DONE_LINE, ("journal",)),
    ],
)
def test_second_boot_needs_the_running_migration_to_have_finished(stack, invocation, journal, fail):
    _render_master(stack, K1)
    host = FakeHost({SALT_SECRET: OLD_SALT, MIGRATE_SECRET: OLD_SALT}, invocation, journal, fail)
    with pytest.raises(ls.Refused, match="has not logged that the migration finished"):
        ls.apply({"LITELLM_SALT_KEY": K1, "LITELLM_MASTER_KEY": MASTER}, stack, host)
    assert host.secrets[SALT_SECRET] == OLD_SALT


@pytest.mark.parametrize("rendered", [None, "sk-some-other-key", "no-master-line"])
def test_second_boot_needs_the_salt_to_be_the_migration_master_key(stack, rendered):
    if rendered == "no-master-line":
        stack.litellm_container.write_text('[Container]\nEnvironment="LITELLM_LOG=INFO"\n', encoding="utf-8")
    elif rendered is not None:
        _render_master(stack, rendered)
    host = FakeHost({SALT_SECRET: OLD_SALT, MIGRATE_SECRET: OLD_SALT}, INVOCATION, DONE_LINE)
    with pytest.raises(ls.Refused, match="LITELLM_SALT_KEY is not the LITELLM_MASTER_KEY of the migration boot"):
        ls.apply({"LITELLM_SALT_KEY": K1, "LITELLM_MASTER_KEY": MASTER}, stack, host)


@pytest.mark.parametrize("journal", [DONE_LINE, f"noise\n{NOTHING_LEFT_LINE}\nmore noise"])
def test_second_migration_boot_replaces_the_salt_secret(stack, journal):
    _render_master(stack, K1)
    _dropin(stack, LEGACY_DROPIN)
    host = FakeHost({SALT_SECRET: OLD_SALT, MIGRATE_SECRET: OLD_SALT}, INVOCATION, journal)
    lines = ls.apply({"LITELLM_SALT_KEY": K1, "LITELLM_MASTER_KEY": MASTER}, stack, host)
    assert any(line.startswith("ℹ️  Second migration boot: LITELLM_SALT_KEY is the master key") for line in lines)
    assert lines[-1] == f"✓ Podman secret {SALT_SECRET} written from LITELLM_SALT_KEY"
    assert host.secrets[SALT_SECRET] == K1
    # prune then removes the key that was migrated from.
    assert ls.prune({"LITELLM_SALT_KEY": K1}, stack, host) == [
        f"✓ Podman secret {MIGRATE_SECRET} removed (LITELLM_MIGRATE_FROM_MASTER_KEY is no longer set)"
    ]
    assert host.secrets == {SALT_SECRET: K1}
    _no_values(lines)


def test_an_unreadable_secret_is_refused(stack):
    host = FakeHost({SALT_SECRET: OLD_SALT}, fail=("inspect-garbage",))
    with pytest.raises(ls.Refused, match=f"cannot read the Podman secret {SALT_SECRET}"):
        ls.apply({"LITELLM_SALT_KEY": OLD_SALT, "LITELLM_MASTER_KEY": MASTER}, stack, host)


def test_a_failed_secret_write_is_refused(stack):
    host = FakeHost(fail=("create",))
    with pytest.raises(ls.Refused, match=f"podman secret create {SALT_SECRET} failed"):
        ls.apply({"LITELLM_SALT_KEY": OLD_SALT, "LITELLM_MASTER_KEY": MASTER}, stack, host)


def test_prune_keeps_the_secret_during_the_migration_and_when_absent(stack):
    host = FakeHost({MIGRATE_SECRET: OLD_SALT})
    assert ls.prune({"LITELLM_MIGRATE_FROM_MASTER_KEY": OLD_SALT}, stack, host) == []
    assert ls.prune({}, stack, FakeHost()) == []
    assert host.secrets == {MIGRATE_SECRET: OLD_SALT}


def test_a_failed_prune_is_reported(stack):
    with pytest.raises(ls.Refused, match=f"podman secret rm {MIGRATE_SECRET} failed"):
        ls.prune({}, stack, FakeHost({MIGRATE_SECRET: OLD_SALT}, fail=("rm",)))


def test_stack_names():
    stack = ls.Stack("llm-routing-dev", Path("/q/llm-routing-dev"), ".env")
    assert stack.salt_name == "llm-routing-dev-litellm-salt-key"
    assert stack.migrate_name == "llm-routing-dev-litellm-migrate-from"
    assert stack.litellm_unit == "llm-routing-dev-litellm.service"
    assert stack.litellm_container == Path("/q/llm-routing-dev/llm-routing-dev-litellm.container")
    assert stack.dropin_dir == Path("/q/llm-routing-dev/llm-routing-dev-litellm.container.d")


def test_run_captures_output_and_feeds_stdin():
    assert ls.run(["true"]).returncode == 0
    fed = ls.run(["cat"], "on stdin only")
    assert (fed.returncode, fed.stdout) == (0, "on stdin only")


def test_main_prints_status_lines(stack, capsys):
    host = FakeHost({SALT_SECRET: OLD_SALT})
    argv = ["apply", "--namespace", NS, "--quadlet-dir", str(stack.quadlet_dir), "--env-file", stack.env_file]
    assert ls.main(argv, {"LITELLM_SALT_KEY": OLD_SALT, "LITELLM_MASTER_KEY": MASTER}, host) == 0
    out = capsys.readouterr()
    assert out.out == f"   ✓ Podman secret {SALT_SECRET} written from LITELLM_SALT_KEY\n"
    argv[0] = "prune"
    assert ls.main(argv, {}, host) == 0


def test_main_reports_a_refusal_on_stderr(stack, capsys):
    argv = ["apply", "--namespace", NS, "--quadlet-dir", str(stack.quadlet_dir)]
    assert ls.main(argv, {"LITELLM_SALT_KEY": MASTER, "LITELLM_MASTER_KEY": MASTER}, FakeHost()) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.startswith("❌ LITELLM_SALT_KEY equals LITELLM_MASTER_KEY")
    assert MASTER not in out.err


def test_main_reads_the_process_environment(stack, monkeypatch, capsys):
    monkeypatch.setenv("LITELLM_SALT_KEY", OLD_SALT)
    monkeypatch.setenv("LITELLM_MASTER_KEY", MASTER)
    monkeypatch.delenv("LITELLM_MIGRATE_FROM_MASTER_KEY", raising=False)
    host = FakeHost()
    assert ls.main(["apply", "--namespace", NS, "--quadlet-dir", str(stack.quadlet_dir)], runner=host) == 0
    assert host.secrets == {SALT_SECRET: OLD_SALT}


def test_main_rejects_an_unknown_action():
    with pytest.raises(SystemExit) as info:
        ls.main(["delete", "--namespace", NS, "--quadlet-dir", "/q"])
    assert info.value.code == 2


# ── End to end: the real CLI against stub podman/systemctl/journalctl ──

STUB_PODMAN = """#!{python}
import json, os, sys
store = os.environ["FAKE_SECRETS"]
secrets = json.load(open(store)) if os.path.exists(store) else {{}}
args = sys.argv[1:]
if args[:2] == ["secret", "inspect"]:
    if args[-1] not in secrets:
        sys.exit(125)
    print(json.dumps([{{"SecretData": secrets[args[-1]]}}]))
elif args[:2] == ["secret", "exists"]:
    sys.exit(0 if args[2] in secrets else 1)
elif args[:2] == ["secret", "create"]:
    secrets[args[3]] = sys.stdin.read()
elif args[:2] == ["secret", "rm"]:
    secrets.pop(args[2])
else:
    sys.exit(2)
json.dump(secrets, open(store, "w"))
"""

STUB_SYSTEMCTL = """#!{python}
import os
print(os.environ.get("FAKE_INVOCATION", ""))
"""

STUB_JOURNALCTL = """#!{python}
import os
print(open(os.environ["FAKE_JOURNAL"]).read())
"""


@pytest.fixture
def host_bin(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, source in (("podman", STUB_PODMAN), ("systemctl", STUB_SYSTEMCTL), ("journalctl", STUB_JOURNALCTL)):
        path = bindir / name
        path.write_text(source.format(python=sys.executable), encoding="utf-8")
        path.chmod(0o755)
    store = tmp_path / "secrets.json"
    store.write_text(json.dumps({SALT_SECRET: OLD_SALT}), encoding="utf-8")
    journal = tmp_path / "journal.txt"
    journal.write_text("", encoding="utf-8")
    return {"bin": bindir, "store": store, "journal": journal}


def _cli(host_bin, stack, action, env):
    base = {k: v for k, v in os.environ.items() if not k.startswith("LITELLM_")}
    base.update(
        PATH=f"{host_bin['bin']}{os.pathsep}{os.environ['PATH']}",
        FAKE_SECRETS=str(host_bin["store"]),
        FAKE_JOURNAL=str(host_bin["journal"]),
        FAKE_INVOCATION=INVOCATION,
        **env,
    )
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), action, "--namespace", NS, "--quadlet-dir", str(stack.quadlet_dir)],
        env=base,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    for value in SECRETS:
        assert value not in proc.stdout + proc.stderr
    return proc


def _store(host_bin):
    return json.loads(host_bin["store"].read_text(encoding="utf-8"))


def test_cli_runs_both_boots_of_a_salt_migration(host_bin, stack):
    legacy = _dropin(stack, LEGACY_DROPIN)
    # Upgrade: the unit passes the secret itself, the legacy drop-in is redundant.
    steady = {"LITELLM_SALT_KEY": OLD_SALT, "LITELLM_MASTER_KEY": MASTER}
    proc = _cli(host_bin, stack, "apply", steady)
    assert proc.returncode == 0 and "is redundant" in proc.stdout

    # Boot 1 with the drop-in still in place: refused before anything changes.
    boot1 = {"LITELLM_MIGRATE_FROM_MASTER_KEY": OLD_SALT, "LITELLM_MASTER_KEY": K1}
    proc = _cli(host_bin, stack, "apply", boot1)
    assert proc.returncode == 1 and "would skip the migration" in proc.stderr
    assert _store(host_bin) == {SALT_SECRET: OLD_SALT}

    # Boot 1: drop-in moved aside, migrate-from secret written, salt secret kept.
    legacy.rename(legacy.with_name(legacy.name + ".migrating"))
    proc = _cli(host_bin, stack, "apply", boot1)
    assert proc.returncode == 0, proc.stderr
    assert _store(host_bin) == {SALT_SECRET: OLD_SALT, MIGRATE_SECRET: OLD_SALT}
    assert _cli(host_bin, stack, "prune", boot1).stdout == ""
    _render_master(stack, K1)  # start-stack.sh renders boot 1's unit

    # Boot 2 before LiteLLM logged the end of the migration: refused.
    boot2 = {"LITELLM_SALT_KEY": K1, "LITELLM_MASTER_KEY": MASTER}
    proc = _cli(host_bin, stack, "apply", boot2)
    assert proc.returncode == 1 and "has not logged that the migration finished" in proc.stderr

    # Boot 2 once it did: the salt secret becomes K1, then the migrate-from secret goes.
    host_bin["journal"].write_text(DONE_LINE + "\n", encoding="utf-8")
    legacy.with_name(legacy.name + ".migrating").rename(legacy)
    proc = _cli(host_bin, stack, "apply", boot2)
    assert proc.returncode == 0, proc.stderr
    assert "Second migration boot" in proc.stdout
    proc = _cli(host_bin, stack, "prune", boot2)
    assert f"{MIGRATE_SECRET} removed" in proc.stdout
    assert _store(host_bin) == {SALT_SECRET: K1}

    # Afterwards the new key is the steady state, and the old one cannot come back by accident.
    assert _cli(host_bin, stack, "apply", boot2).returncode == 0
    proc = _cli(host_bin, stack, "apply", steady)
    assert proc.returncode == 1 and f"differs from the Podman secret {SALT_SECRET}" in proc.stderr
