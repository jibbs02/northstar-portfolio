from __future__ import annotations

import os
from pathlib import Path

import pytest

from northstar.config import Settings
from northstar.data import Database
from northstar.operations.replication import ReplicationError, ReplicationService
from northstar.services import BackupService


def replication_stack(settings: Settings, tmp_path: Path) -> tuple[ReplicationService, Path, Path, Path]:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    database.initialize()
    database.upsert_secure_record("demo", "replication", {"synthetic": True})
    backup = BackupService(database, settings.backup_dir, settings.backup_vault()).create()
    destination = tmp_path / "secondary"
    destination.mkdir(mode=0o700)
    destination.chmod(0o700)
    service = ReplicationService(
        settings.backup_dir,
        destination,
        settings.backup_vault(),
        repository_root=Path(__file__).resolve().parents[1],
        runtime_root=settings.database_path.parent,
    )
    return service, backup, destination, settings.database_path


def test_replication_copies_only_authenticated_finalized_backups_atomically(
    settings: Settings, tmp_path: Path
) -> None:
    service, backup, destination, database = replication_stack(settings, tmp_path)
    (settings.backup_dir / "ignored.csv").write_text("synthetic only", encoding="utf-8")
    (settings.backup_dir / "northstar.db.restore").write_bytes(b"synthetic")

    result = service.run()
    verified = service.run(verify_only=True)

    assert result.copied_count == 1
    assert verified.verified_count == 1
    assert {path.name for path in destination.iterdir()} == {backup.name}
    assert (destination / backup.name).read_bytes() == backup.read_bytes()
    assert (destination / backup.name).stat().st_mode & 0o777 == 0o600
    assert database.exists()


def test_replication_dry_run_does_not_write(settings: Settings, tmp_path: Path) -> None:
    service, _, destination, _ = replication_stack(settings, tmp_path)

    result = service.run(dry_run=True)

    assert result.source_count == 1
    assert result.copied_count == 0
    assert not list(destination.iterdir())


def test_replication_rejects_hash_mismatch_without_deleting_other_recovery_point(
    settings: Settings, tmp_path: Path
) -> None:
    service, backup, destination, _ = replication_stack(settings, tmp_path)
    existing = destination / "northstar_20260101T010101.000000Z.nsbak"
    existing.write_bytes(b"previous-synthetic-recovery-point")
    existing.chmod(0o600)
    corrupted = destination / backup.name
    corrupted.write_bytes(b"X" * backup.stat().st_size)
    corrupted.chmod(0o600)

    with pytest.raises(ReplicationError, match="hash"):
        service.run()

    assert existing.read_bytes() == b"previous-synthetic-recovery-point"
    assert corrupted.exists()


def test_replication_cleans_partial_files_and_interrupted_copy(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, backup, destination, _ = replication_stack(settings, tmp_path)
    stale = destination / f".{backup.name}.stale.partial"
    stale.write_bytes(b"partial")
    stale.chmod(0o600)

    def interrupt(_: Path, __: Path) -> None:
        raise OSError("synthetic interruption")

    monkeypatch.setattr("northstar.operations.replication.os.replace", interrupt)
    with pytest.raises(OSError, match="synthetic interruption"):
        service.run()

    assert not stale.exists()
    assert not (destination / backup.name).exists()
    assert not list(destination.glob("*.partial"))


def test_replication_rejects_unavailable_symlinked_and_prohibited_destinations(
    settings: Settings, tmp_path: Path
) -> None:
    service, _, destination, _ = replication_stack(settings, tmp_path)
    unavailable = replace_destination(service, tmp_path / "missing")
    real = tmp_path / "real-secondary"
    real.mkdir(mode=0o700)
    symlink = tmp_path / "linked-secondary"
    symlink.symlink_to(real, target_is_directory=True)
    linked = replace_destination(service, symlink)
    prohibited = replace_destination(service, settings.database_path.parent / "secondary")

    with pytest.raises(ReplicationError, match="unavailable"):
        unavailable.run()
    with pytest.raises(ReplicationError, match="permissions"):
        linked.run()
    with pytest.raises(ReplicationError, match="prohibited"):
        prohibited.run()
    assert not list(destination.iterdir())


def test_replication_rejects_unauthenticated_finalized_backup(settings: Settings, tmp_path: Path) -> None:
    service, backup, _, _ = replication_stack(settings, tmp_path)
    backup.write_bytes(os.urandom(128))

    with pytest.raises(ReplicationError, match="authentication"):
        service.run()


def test_replication_rejects_symlinked_source(settings: Settings, tmp_path: Path) -> None:
    service, _, _, _ = replication_stack(settings, tmp_path)
    linked_source = tmp_path / "linked-backups"
    linked_source.symlink_to(service.source_dir, target_is_directory=True)
    linked = ReplicationService(
        linked_source,
        service.destination_dir,
        service.backup_vault,
        repository_root=service.repository_root,
        runtime_root=service.runtime_root,
    )

    with pytest.raises(ReplicationError, match="source permissions"):
        linked.run()


def replace_destination(service: ReplicationService, destination: Path) -> ReplicationService:
    return ReplicationService(
        service.source_dir,
        destination,
        service.backup_vault,
        repository_root=service.repository_root,
        runtime_root=service.runtime_root,
    )
