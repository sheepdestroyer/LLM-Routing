"""upgrade-prod.sh restarts the host units whose files a release changed.

agy-daemon.service runs scripts/host_agy_daemon.py on the host, outside the pod. A running process
keeps the code it started with, and start-stack.sh restarts the daemon only when it rewrites the
unit file, so a release that changed only the script left the old code running (prod ran 11-03
code across several releases). These tests run the actual script in a temporary PROD_DIR against
recording stubs: git (clone = copy a release tree), systemctl (a stateful fake user manager), sleep,
and the release's start-stack.sh.
"""

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "upgrade-prod.sh"
UNIT = "agy-daemon.service"
DAEMON = "scripts/host_agy_daemon.py"
RESTART = f"systemctl --user restart {UNIT} --no-pager"
RELOAD = "systemctl --user daemon-reload --no-pager"

GIT_STUB = """#!/bin/sh
printf 'git %s\\n' "$*" >> "$CALL_LOG"
[ "$1" = clone ] || exit 1
for dest; do :; done
cp -a "$RELEASE_DIR/." "$dest"
"""

# The user manager: unit state in files under $SYSTEMCTL_STATE.
SYSTEMCTL_STUB = """#!/bin/sh
printf 'systemctl %s\\n' "$*" >> "$CALL_LOG"
s="$SYSTEMCTL_STATE"
case "$*" in
    *--property=ActiveState*) cat "$s/active_state" ;;
    *--property=ActiveEnterTimestampMonotonic*) cat "$s/since" ;;
    *--property=NeedDaemonReload*) cat "$s/need_reload" ;;
    *--property=MainPID*) cat "$s/main_pid" ;;
    *daemon-reload*) echo no > "$s/need_reload" ;;
    *" restart "*)
        rc=$(cat "$s/restart_rc")
        [ "$rc" = 0 ] || exit "$rc"
        echo $(( $(cat "$s/since") + 1 )) > "$s/since"
        echo $(( $(cat "$s/main_pid") + 1 )) > "$s/main_pid"
        if [ -f "$s/crash" ]; then echo activating > "$s/active_state"; else echo active > "$s/active_state"; fi
        ;;
esac
"""

# The release's start-stack.sh: like the real one, it rewrites the agy-daemon unit when it differs,
# then reloads and restarts it (each step can be switched off to model a swallowed failure).
START_STACK_STUB = """#!/bin/bash
printf 'start-stack.sh %s\\n' "$*" >> "$CALL_LOG"
unit="$HOME/.config/systemd/user/agy-daemon.service"
if [ -n "${NEW_UNIT:-}" ] && [ "$(cat "$unit" 2>/dev/null)" != "$NEW_UNIT" ]; then
    printf '%s\\n' "$NEW_UNIT" > "$unit"
    echo yes > "$SYSTEMCTL_STATE/need_reload"
    [ "${START_STACK_RELOADS:-1}" = 1 ] && systemctl --user daemon-reload --no-pager
    [ "${START_STACK_RESTARTS:-1}" = 1 ] && systemctl --user restart agy-daemon.service --no-pager
fi
exit "${START_STACK_RC:-0}"
"""

# Different sizes: the trees are written within the same second, and rsync skips a file whose size
# and mtime match (a real release is a fresh clone, so its mtimes always differ).
OLD_CODE = "print('daemon v1')\n"
NEW_CODE = "print('daemon v2, fixed')\n"
OLD_UNIT = "[Service]\nExecStart=/usr/bin/python3 host_agy_daemon.py\n"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def write_tree(base: Path, daemon_code: str, upgrade_script: str) -> None:
    """A runtime tree as a release (and prod) holds it."""
    for directory in ("quadlets", "litellm", "router", "scripts"):
        (base / directory).mkdir(parents=True, exist_ok=True)
    (base / "pod.yaml").write_text("kind: Pod\n")
    (base / "docker-compose.yml").write_text("services: {}\n")
    (base / "start-stack.sh").write_text(START_STACK_STUB)
    (base / DAEMON).write_text(daemon_code)
    (base / "scripts" / "upgrade-prod.sh").write_text(upgrade_script)


class Upgrade:
    """A prod directory, a release and a fake user manager; run() executes upgrade-prod.sh."""

    def __init__(self, tmp_path: Path):
        self.prod = tmp_path / "prod"
        self.release = tmp_path / "release"
        self.home = tmp_path / "home"
        self.unit = self.home / ".config" / "systemd" / "user" / UNIT
        self.manager = tmp_path / "systemctl-state"
        self.log = tmp_path / "calls.log"
        self.bin = tmp_path / "bin"
        write_tree(self.prod, OLD_CODE, "# upgrade-prod.sh of the previous release\n")
        (self.prod / ".env").write_text(
            "OPENROUTER_API_KEY=test\nOLLAMA_API_KEY=test\nLLAMA_CLASSIFIER_URL=http://127.0.0.1:1/v1\n"
        )
        write_tree(self.release, NEW_CODE, SCRIPT.read_text())
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text(OLD_UNIT)
        self.manager.mkdir()
        self.set_manager(active_state="active", since="100", need_reload="no", main_pid="4242", restart_rc="0")
        self.bin.mkdir()
        for name, body in (("git", GIT_STUB), ("systemctl", SYSTEMCTL_STUB), ("sleep", "#!/bin/sh\nexit 0\n")):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)

    def set_manager(self, **values: str) -> None:
        for name, value in values.items():
            (self.manager / name).write_text(value + "\n")

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        self.log.write_text("")
        full_env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("UPGRADE_PROD_")},
            "HOME": str(self.home),
            "PROD_DIR": str(self.prod),
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "CALL_LOG": str(self.log),
            "RELEASE_DIR": str(self.release),
            "SYSTEMCTL_STATE": str(self.manager),
            **env,
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *(args or ("v9.9.9",))],
            env=full_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def recorded(self) -> dict[str, str]:
        state = self.prod / ".host-units.sha256"
        return {path: digest for digest, path in (line.split(" ", 1) for line in state.read_text().splitlines())}


@pytest.fixture
def upgrade(tmp_path) -> Upgrade:
    return Upgrade(tmp_path)


def test_release_changing_only_the_daemon_script_restarts_it_before_the_redeploy(upgrade):
    res = upgrade.run()
    assert res.returncode == 0, res.stderr
    calls = upgrade.calls()
    assert calls.count(RESTART) == 1
    assert RELOAD not in calls
    # Restarted right after the sync: the daemon never runs old code next to the new files.
    assert calls.index(RESTART) < calls.index("start-stack.sh --pull")
    assert f"♻️  Restarting {UNIT} (changed: {DAEMON})" in res.stdout
    assert f"✓ Restarted {UNIT}: active, PID 4243" in res.stdout
    assert f"♻️  Host units restarted by this upgrade: {UNIT}" in res.stdout
    assert (upgrade.prod / DAEMON).read_text() == NEW_CODE
    assert upgrade.recorded() == {str(upgrade.unit): sha256(OLD_UNIT), str(upgrade.prod / DAEMON): sha256(NEW_CODE)}

    # Idempotent: the same release again restarts nothing.
    again = upgrade.run()
    assert again.returncode == 0, again.stderr
    assert RESTART not in upgrade.calls()
    assert f"✓ {UNIT}: unchanged, not restarted" in again.stdout
    assert "✓ No host unit restarted by this upgrade" in again.stdout


def test_release_without_daemon_changes_does_not_restart_it(upgrade):
    write_tree(upgrade.release, OLD_CODE, SCRIPT.read_text())
    res = upgrade.run()
    assert res.returncode == 0, res.stderr
    assert not [call for call in upgrade.calls() if " restart " in call or "daemon-reload" in call]
    assert "✓ No host unit restarted by this upgrade" in res.stdout


def test_unit_file_regenerated_without_reload_is_reloaded_and_restarted(upgrade):
    # start-stack.sh rewrote the unit, but its daemon-reload and restart failed silently (|| true).
    write_tree(upgrade.release, OLD_CODE, SCRIPT.read_text())
    res = upgrade.run(NEW_UNIT="[Service]\nExecStart=new", START_STACK_RELOADS="0", START_STACK_RESTARTS="0")
    assert res.returncode == 0, res.stderr
    calls = upgrade.calls()
    assert calls.index("start-stack.sh --pull") < calls.index(RELOAD) < calls.index(RESTART)
    assert calls.count(RESTART) == 1
    assert f"♻️  Restarting {UNIT} (changed: {upgrade.unit})" in res.stdout
    assert upgrade.recorded()[str(upgrade.unit)] == sha256("[Service]\nExecStart=new\n")


@pytest.mark.parametrize("daemon_code", [OLD_CODE, NEW_CODE])
def test_restart_by_start_stack_is_not_repeated(upgrade, daemon_code):
    write_tree(upgrade.release, daemon_code, SCRIPT.read_text())
    res = upgrade.run(NEW_UNIT="[Service]\nExecStart=new")
    assert res.returncode == 0, res.stderr
    calls = upgrade.calls()
    # One restart by start-stack.sh for the unit file, plus one before it when the script changed.
    assert calls.count(RESTART) == (2 if daemon_code == NEW_CODE else 1)
    assert calls[-1] != RESTART
    assert f"✓ {UNIT} already restarted during this upgrade (changed: {upgrade.unit})" in res.stdout
    assert RELOAD in calls  # start-stack.sh's own reload, none needed afterwards
    assert calls.count(RELOAD) == 1


def test_failed_redeploy_still_restarts_changed_units_and_fails(upgrade):
    res = upgrade.run(START_STACK_RC="3")
    assert res.returncode == 3
    assert upgrade.calls().count(RESTART) == 1
    assert "start-stack.sh --pull failed (exit 3)" in res.stderr
    assert upgrade.recorded()[str(upgrade.prod / DAEMON)] == sha256(NEW_CODE)


def test_failed_restart_keeps_the_old_digest_and_the_next_run_restarts(upgrade):
    upgrade.set_manager(restart_rc="1")
    res = upgrade.run()
    assert res.returncode == 1
    # Tried before the redeploy and again after it.
    assert upgrade.calls().count(RESTART) == 2
    assert f"Failed to restart {UNIT}" in res.stderr
    assert upgrade.recorded()[str(upgrade.prod / DAEMON)] == sha256(OLD_CODE)

    upgrade.set_manager(restart_rc="0")
    again = upgrade.run()
    assert again.returncode == 0, again.stderr
    assert upgrade.calls().count(RESTART) == 1
    assert f"♻️  Restarting {UNIT} (changed: {DAEMON})" in again.stdout
    assert upgrade.recorded()[str(upgrade.prod / DAEMON)] == sha256(NEW_CODE)


def test_unit_crashing_on_the_new_files_fails_the_upgrade(upgrade):
    (upgrade.manager / "crash").write_text("")
    res = upgrade.run()
    assert res.returncode == 1
    assert upgrade.calls().count(RESTART) == 1  # restarting again would not help
    assert f"{UNIT} is not running after the restart" in res.stderr
    assert upgrade.recorded()[str(upgrade.prod / DAEMON)] == sha256(NEW_CODE)


def test_stopped_unit_is_not_started_by_the_upgrade(upgrade):
    upgrade.set_manager(active_state="inactive")
    res = upgrade.run()
    assert res.returncode == 0, res.stderr
    assert RESTART not in upgrade.calls()
    assert f"ℹ️  {UNIT} is inactive: it runs the new files when it starts (changed: {DAEMON})" in res.stdout
    assert upgrade.recorded()[str(upgrade.prod / DAEMON)] == sha256(NEW_CODE)


def test_unreachable_user_manager_fails_and_keeps_the_old_digest(upgrade):
    upgrade.set_manager(active_state="")
    res = upgrade.run()
    assert res.returncode == 1
    assert RESTART not in upgrade.calls()
    assert f"Cannot read the state of {UNIT}" in res.stderr
    assert upgrade.recorded()[str(upgrade.prod / DAEMON)] == sha256(OLD_CODE)


def test_failed_daemon_reload_skips_the_restart(upgrade):
    upgrade.set_manager(need_reload="yes")
    (upgrade.bin / "systemctl").write_text(
        SYSTEMCTL_STUB.replace("*daemon-reload*) echo no", "*daemon-reload*) exit 1; echo no")
    )
    res = upgrade.run()
    assert res.returncode == 1
    assert RESTART not in upgrade.calls()
    assert "daemon-reload failed" in res.stderr


def test_dry_run_reports_the_restart_and_changes_nothing(upgrade):
    res = upgrade.run("--dry-run", "v9.9.9")
    assert res.returncode == 0, res.stderr
    assert f"  {UNIT} would be restarted (changed: {DAEMON})" in res.stdout
    assert "  upgrade-prod.sh (root copy) differs" not in res.stdout  # no root copy in this prod
    assert not [call for call in upgrade.calls() if call.startswith("systemctl")]
    assert (upgrade.prod / DAEMON).read_text() == OLD_CODE
    assert not (upgrade.prod / ".host-units.sha256").exists()

    write_tree(upgrade.release, OLD_CODE, SCRIPT.read_text())
    unchanged = upgrade.run("--dry-run", "v9.9.9")
    assert f"  {UNIT}: files unchanged" in unchanged.stdout


def test_root_copy_of_the_script_follows_the_release(upgrade):
    (upgrade.prod / "upgrade-prod.sh").write_text("# root copy of the previous release\n")
    assert "  upgrade-prod.sh (root copy) differs" in upgrade.run("--dry-run", "v9.9.9").stdout
    res = upgrade.run()
    assert res.returncode == 0, res.stderr
    assert (upgrade.prod / "upgrade-prod.sh").read_text() == SCRIPT.read_text()
    assert (upgrade.prod / "scripts" / "upgrade-prod.sh").read_text() == SCRIPT.read_text()


def test_no_root_copy_is_created(upgrade):
    assert upgrade.run().returncode == 0
    assert not (upgrade.prod / "upgrade-prod.sh").exists()


def test_every_host_unit_start_stack_writes_is_tracked():
    """A unit start-stack.sh installs for a PROD_DIR script must be in HOST_UNITS with that script."""
    start_stack = (ROOT / "start-stack.sh").read_text()
    written = set(re.findall(r"\.config/systemd/user/([\w@.-]+\.service)", start_stack))
    assert written == {UNIT}
    match = re.search(r"^HOST_UNITS=\((.*)\)$", SCRIPT.read_text(), re.MULTILINE)
    assert match is not None
    tracked = dict(entry.strip('"').split(":", 1) for entry in match.group(1).split())
    assert set(tracked) == written
    assert 'AGY_DAEMON_SCRIPT="${WORKDIR}/scripts/host_agy_daemon.py"' in start_stack
    assert tracked[UNIT] == DAEMON
