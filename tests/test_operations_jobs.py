from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from northstar.config import Settings
from northstar.data import Database
from northstar.data.database import LATEST_SCHEMA_VERSION
from northstar.operations.jobs import ExclusiveJobLock, JobLockedError, MonthlyRecoveryJob, ScheduledBackupJob
from northstar.operations.state import OperationalStateStore
from northstar.services import BackupError, BackupService


def job_stack(
    settings: Settings, tmp_path: Path, *, clock: object | None = None
) -> tuple[Database, BackupService, OperationalStateStore, Path]:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    database.initialize()
    database.upsert_secure_record("demo", "jobs", {"synthetic": True})
    operations = tmp_path / "operations"
    operations.mkdir(mode=0o700)
    operations.chmod(0o700)
    service = BackupService(
        database,
        settings.backup_dir,
        settings.backup_vault(),
        clock=clock,  # type: ignore[arg-type]
    )
    return database, service, OperationalStateStore(operations), operations


def test_concurrent_backup_attempt_fails_without_creating_another_backup(
    settings: Settings, tmp_path: Path
) -> None:
    _, service, state, operations = job_stack(settings, tmp_path)
    job = ScheduledBackupJob(service, state, operations)

    with ExclusiveJobLock(operations, "backup"):
        with pytest.raises(JobLockedError):
            job.run()

    assert not list(settings.backup_dir.glob("*.nsbak"))
    assert state.read().last_failure_code == "backup_already_running"


def test_failed_backup_never_runs_retention_or_records_success(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, service, state, operations = job_stack(settings, tmp_path)
    prune_called = False

    def fail_drill(_: Path | None = None, **__: object) -> None:
        raise BackupError("synthetic drill failure")

    def record_prune() -> list[Path]:
        nonlocal prune_called
        prune_called = True
        return []

    monkeypatch.setattr(service, "restore_drill", fail_drill)
    monkeypatch.setattr(service, "prune", record_prune)

    with pytest.raises(BackupError):
        ScheduledBackupJob(service, state, operations).run()

    assert not prune_called
    assert state.read().last_successful_backup is None
    assert state.read().last_failure_code == "backup_job_failed"
    assert not list(settings.backup_dir.glob("*.nsbak"))


def test_monthly_recovery_selects_newest_and_never_touches_production(
    settings: Settings, tmp_path: Path
) -> None:
    current = datetime(2026, 7, 1, 1, tzinfo=UTC)

    def clock() -> datetime:
        return current

    database, service, state, operations = job_stack(settings, tmp_path, clock=clock)
    first = service.create()
    current = datetime(2026, 8, 1, 1, tzinfo=UTC)
    database.upsert_secure_record("demo", "newer", {"synthetic": "newer"})
    newest = service.create()
    before = settings.database_path.read_bytes()
    before_stat = settings.database_path.stat()

    result = MonthlyRecoveryJob(service, state, operations).run()

    after_stat = settings.database_path.stat()
    assert first.exists() and newest.exists()
    assert result.schema_version == LATEST_SCHEMA_VERSION
    assert result.backup_path.name == "validated.nsbak"
    assert settings.database_path.read_bytes() == before
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert state.read().last_successful_restore_drill is not None
    assert not list(operations.glob("recovery-drill-*"))


def test_operational_state_write_is_atomic_on_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "operations"
    directory.mkdir(mode=0o700)
    store = OperationalStateStore(directory)
    store.success("backup", when=datetime(2026, 8, 1, tzinfo=UTC))
    before = store.path.read_bytes()

    def interrupt(_: Path, __: Path) -> None:
        raise OSError("synthetic status interruption")

    monkeypatch.setattr("northstar.operations.state.os.replace", interrupt)
    with pytest.raises(OSError, match="synthetic status interruption"):
        store.success("replication")

    assert store.path.read_bytes() == before
    assert not list(directory.glob("*.tmp"))


def test_job_lock_rejects_symlinked_operations_directory(tmp_path: Path) -> None:
    real = tmp_path / "real-operations"
    real.mkdir(mode=0o700)
    linked = tmp_path / "operations"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="permissions are unsafe"):
        with ExclusiveJobLock(linked, "backup"):
            pass

    assert not (real / "backup.lock").exists()
