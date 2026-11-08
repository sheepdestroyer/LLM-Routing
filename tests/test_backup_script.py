"""Unit tests for scripts/backup.sh: prune logic and owner-only backups."""

import os
import shutil
import stat
import subprocess
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backup.sh"

# Stand-in for podman: postgres is running and ready, pg_dump writes a few bytes, or fails when
# $PG_DUMP_FAIL is set (after writing a partial dump, like a dump cut short).
PODMAN_STUB = """#!/bin/sh
case "$1" in
    inspect) echo true ;;
    exec)
        case "$*" in
            *pg_isready*) exit 0 ;;
            *pg_dump*)
                echo "PGDMP partial"
                [ -n "$PG_DUMP_FAIL" ] && exit 1
                exit 0 ;;
        esac ;;
esac
"""

CONFIG_FILES = (
    "litellm/config.yaml",
    "router/config.yaml",
    "router/main.py",
    "router/memory_mcp.py",
    "router/agentic_scores.json",
    "pod.yaml",
)


def _workdir(tmp_path: Path) -> Path:
    """A release tree with the files backup.sh snapshots, plus a .env holding a secret."""
    workdir = tmp_path / "LLM-Routing"
    (workdir / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, workdir / "scripts" / "backup.sh")
    for name in CONFIG_FILES:
        path = workdir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {name}\n")
    (workdir / ".env").write_text("POD_NAME=test-pod\nPOSTGRES_PORT=5432\nLITELLM_MASTER_KEY=sk-test-secret\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    podman = bindir / "podman"
    podman.write_text(PODMAN_STUB)
    podman.chmod(0o755)
    return workdir


def _run_backup(workdir: Path, **env_extra: str) -> subprocess.CompletedProcess:
    """Run the real script under a permissive caller umask (022), as cron or a login shell would."""
    env = {
        "PATH": f"{workdir.parent / 'bin'}:{os.environ['PATH']}",
        "HOME": str(workdir.parent),
        **env_extra,
    }
    return subprocess.run(
        ["bash", "-c", 'umask 022 && exec bash "$1"', "backup", str(workdir / "scripts" / "backup.sh")],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_backups_are_owner_only_under_a_permissive_umask(tmp_path: Path):
    workdir = _workdir(tmp_path)
    result = _run_backup(workdir)
    assert result.returncode == 0, result.stdout + result.stderr

    backups = workdir / "backups"
    assert _mode(backups) == 0o700
    files = sorted(p.name for p in backups.iterdir())
    assert [n.split("_db_")[0] for n in files if n.endswith(".dump")] == ["langfuse", "postgres"]
    (snapshot,) = [p for p in backups.iterdir() if p.name.endswith(".tar.gz")]
    with tarfile.open(snapshot) as tar:
        assert ".env" in tar.getnames()  # the secrets are in there: this is why the mode matters
    for path in backups.iterdir():
        assert _mode(path) == 0o600, f"{path.name} is {oct(_mode(path))}"


def test_existing_backups_dir_and_old_backups_are_tightened(tmp_path: Path):
    workdir = _workdir(tmp_path)
    backups = workdir / "backups"
    backups.mkdir(mode=0o755)
    backups.chmod(0o755)
    legacy = {
        name: backups / name
        for name in ("configs_20261101_000000.tar.gz", "postgres_db_20261101_000000.dump", "notes.txt")
    }
    for path in legacy.values():
        path.write_text("old")
        path.chmod(0o644)

    result = _run_backup(workdir)
    assert result.returncode == 0, result.stdout + result.stderr

    assert _mode(backups) == 0o700
    assert _mode(legacy["configs_20261101_000000.tar.gz"]) == 0o600
    assert _mode(legacy["postgres_db_20261101_000000.dump"]) == 0o600
    assert _mode(legacy["notes.txt"]) == 0o644  # not a backup.sh file: left alone
    for path in backups.iterdir():
        if path.name.endswith((".dump", ".tar.gz")):
            assert _mode(path) == 0o600, f"{path.name} is {oct(_mode(path))}"


def test_a_failed_dump_leaves_no_readable_partial_file(tmp_path: Path):
    workdir = _workdir(tmp_path)
    result = _run_backup(workdir, PG_DUMP_FAIL="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAILED" in result.stdout

    dumps = [p for p in (workdir / "backups").iterdir() if p.name.endswith(".dump")]
    assert len(dumps) == 2
    for path in dumps:
        assert _mode(path) == 0o600


def test_backup_pruning_operator_precedence(tmp_path: Path):
    """Verify that backup prune deletes both .dump and .tar.gz files older than RETENTION_DAYS

    while preserving newer files.
    """
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    now = time.time()
    old_time = now - (15 * 86400)  # 15 days old (> 14 days)
    new_time = now - (2 * 86400)  # 2 days old (< 14 days)

    # Create test files
    old_dump = backup_dir / "postgres_db_20260101_000000.dump"
    new_dump = backup_dir / "postgres_db_20260813_000000.dump"
    old_tar = backup_dir / "configs_20260101_000000.tar.gz"
    new_tar = backup_dir / "configs_20260813_000000.tar.gz"

    for p in [old_dump, new_dump, old_tar, new_tar]:
        p.write_text("test_content")

    # Set timestamps
    os.utime(old_dump, (old_time, old_time))
    os.utime(old_tar, (old_time, old_time))
    os.utime(new_dump, (new_time, new_time))
    os.utime(new_tar, (new_time, new_time))

    # Run find command matching the fixed backup.sh logic
    cmd = ["find", str(backup_dir), "(", "-name", "*.dump", "-o", "-name", "*.tar.gz", ")", "-mtime", "+14", "-delete"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0

    # Old files must be deleted
    assert not old_dump.exists()
    assert not old_tar.exists()

    # New files must remain intact
    assert new_dump.exists()
    assert new_tar.exists()
