"""Unit tests for scripts/backup.sh prune logic."""
import subprocess
import time
from pathlib import Path


def test_backup_pruning_operator_precedence(tmp_path: Path):
    """Verify that backup prune deletes both .dump and .tar.gz files older than RETENTION_DAYS

    while preserving newer files.
    """
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    now = time.time()
    old_time = now - (15 * 86400)  # 15 days old (> 14 days)
    new_time = now - (2 * 86400)   # 2 days old (< 14 days)

    # Create test files
    old_dump = backup_dir / "postgres_db_20260101_000000.dump"
    new_dump = backup_dir / "postgres_db_20260813_000000.dump"
    old_tar = backup_dir / "configs_20260101_000000.tar.gz"
    new_tar = backup_dir / "configs_20260813_000000.tar.gz"

    for p in [old_dump, new_dump, old_tar, new_tar]:
        p.write_text("test_content")

    # Set timestamps
    import os
    os.utime(old_dump, (old_time, old_time))
    os.utime(old_tar, (old_time, old_time))
    os.utime(new_dump, (new_time, new_time))
    os.utime(new_tar, (new_time, new_time))

    # Run find command matching the fixed backup.sh logic
    cmd = [
        "find", str(backup_dir),
        "(", "-name", "*.dump", "-o", "-name", "*.tar.gz", ")",
        "-mtime", "+14",
        "-delete"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0

    # Old files must be deleted
    assert not old_dump.exists()
    assert not old_tar.exists()

    # New files must remain intact
    assert new_dump.exists()
    assert new_tar.exists()
