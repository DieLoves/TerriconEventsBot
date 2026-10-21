from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def make_fake_docker(tmp_path: Path, body: str) -> Path:
    executable = tmp_path / "bin" / "docker"
    executable.parent.mkdir()
    executable.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable.parent


def run_script(script: str, *, env: dict[str, str], argument: str | None = None) -> str:
    command = [str(PROJECT_ROOT / "scripts" / script)]
    if argument is not None:
        command.append(argument)
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_backup_script_creates_private_archive_and_rotates_to_seven(tmp_path: Path) -> None:
    fake_bin = make_fake_docker(
        tmp_path,
        """
if [ "$1" = "compose" ]; then
    printf 'custom-format-dump\\n'
    exit 0
fi
exit 2
""",
    )
    backup_dir = tmp_path / "backups"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "BACKUP_DIR": str(backup_dir),
    }

    outputs = [run_script("backup.sh", env=env) for _ in range(8)]

    archives = sorted(backup_dir.glob("terricon-*.dump"))
    assert len(archives) == 7
    assert outputs[-1] == str(archives[-1])
    assert all(path.read_text(encoding="utf-8") == "custom-format-dump\n" for path in archives)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in archives)
    assert backup_dir.stat().st_mode & 0o777 == 0o700


def test_restore_smoke_uses_isolated_container_and_cleans_it_up(tmp_path: Path) -> None:
    log_path = tmp_path / "docker.log"
    fake_bin = make_fake_docker(
        tmp_path,
        """
printf '%s\\n' "$*" >>"$FAKE_DOCKER_LOG"
case "$*" in
    run*) printf 'container-id\\n' ;;
    *pg_isready*) exit 0 ;;
    *pg_restore*) exit 0 ;;
    *version_num*) printf '0010_hardening_queue_state\\n' ;;
    *information_schema.tables*) printf '24\\n' ;;
    'rm --force'*) exit 0 ;;
    *) exit 2 ;;
esac
""",
    )
    archive = tmp_path / "backup.dump"
    archive.write_bytes(b"custom-format-dump")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log_path),
    }

    output = run_script("restore-smoke.sh", env=env, argument=str(archive))

    calls = log_path.read_text(encoding="utf-8")
    assert output == "Restore smoke OK: revision 0010_hardening_queue_state, public tables 24."
    assert "run --detach" in calls
    assert "pg_restore --exit-on-error" in calls
    assert "rm --force terricon-restore-smoke-" in calls


def test_restore_smoke_rejects_an_outdated_schema(tmp_path: Path) -> None:
    fake_bin = make_fake_docker(
        tmp_path,
        """
case "$*" in
    run*) printf 'container-id\\n' ;;
    *pg_isready*) exit 0 ;;
    *pg_restore*) exit 0 ;;
    *version_num*) printf '0009_stage7_broadcast_state\\n' ;;
    *information_schema.tables*) printf '24\\n' ;;
    'rm --force'*) exit 0 ;;
    *) exit 2 ;;
esac
""",
    )
    archive = tmp_path / "backup.dump"
    archive.write_bytes(b"custom-format-dump")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    with pytest.raises(subprocess.CalledProcessError) as caught:
        run_script("restore-smoke.sh", env=env, argument=str(archive))

    assert "expected 0010_hardening_queue_state" in caught.value.stderr
