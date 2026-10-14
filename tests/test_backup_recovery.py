from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from northstar.config import Settings
from northstar.data import Database, DatabaseEncryptionError
from northstar.data.database import MIGRATION_2, MIGRATION_3, SCHEMA_V1, utc_now
from northstar.services import BackupError, BackupRetentionPolicy, BackupService
from northstar.services.backup import BACKUP_AAD
from scripts import restore_backup, verify_backup_restore


def initialized_database(settings: Settings) -> Database:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    database.initialize()
    database.upsert_secure_record("demo", "source", {"safe": True})
    return database


def backup_service(
    settings: Settings,
    database: Database,
    retention: BackupRetentionPolicy | None = None,
) -> BackupService:
    return BackupService(
        database,
        settings.backup_dir,
        settings.backup_vault(),
        retention,
    )


def test_plaintext_sqlite_database_is_rejected(settings: Settings) -> None:
    settings.database_path.parent.mkdir(parents=True)
    with sqlite3.connect(settings.database_path) as plaintext:
        plaintext.execute("CREATE TABLE forbidden(value TEXT)")

    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    with pytest.raises(DatabaseEncryptionError, match="plaintext"):
        database.initialize()
    assert settings.database_path.stat().st_mode & 0o777 == 0o600


def test_backup_refuses_uninitialized_source_without_creating_database(settings: Settings) -> None:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    service = backup_service(settings, database)

    with pytest.raises(BackupError, match="health validation"):
        service.create()

    assert not settings.database_path.exists()
    assert not settings.backup_dir.exists()


def test_wrong_database_key_cannot_open_whole_database(settings: Settings) -> None:
    database = initialized_database(settings)
    wrong = Database(settings.database_path, settings.data_vault(), os.urandom(32))

    assert database.health()
    assert not wrong.health()


def test_stale_restore_symlink_is_removed_without_touching_database(settings: Settings) -> None:
    database = initialized_database(settings)
    service = backup_service(settings, database)
    original = settings.database_path.read_bytes()
    artifact = service.restore_artifact(settings.database_path)
    artifact.symlink_to(settings.database_path)

    assert service.remove_stale_restore(settings.database_path)
    assert not artifact.exists()
    assert settings.database_path.read_bytes() == original
    assert database.get_secure_record("demo", "source") == {"safe": True}


def test_unexpected_restore_directory_is_never_removed(settings: Settings) -> None:
    database = initialized_database(settings)
    service = backup_service(settings, database)
    original = settings.database_path.read_bytes()
    artifact = service.restore_artifact(settings.database_path)
    artifact.mkdir()

    with pytest.raises(BackupError, match="unexpected restore artifact"):
        service.remove_stale_restore(settings.database_path)

    assert artifact.is_dir()
    assert settings.database_path.read_bytes() == original


def test_restore_replace_failure_preserves_valid_database(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = initialized_database(settings)
    service = backup_service(settings, source)
    backup = service.create()
    destination = tmp_path / "recovery" / "northstar.db"
    existing = Database(destination, settings.data_vault(), settings.database_key)
    existing.initialize()
    existing.upsert_secure_record("demo", "existing", {"preserved": True})
    before = destination.read_bytes()

    def fail_replace(_: Path, __: Path) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr("northstar.services.backup.os.replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        service.restore_to(backup, destination)

    assert destination.read_bytes() == before
    assert existing.get_secure_record("demo", "existing") == {"preserved": True}
    assert not service.restore_artifact(destination).exists()


def test_invalid_encrypted_database_never_replaces_destination(settings: Settings, tmp_path: Path) -> None:
    source = initialized_database(settings)
    service = backup_service(settings, source)
    invalid = tmp_path / "invalid.nsbak"
    invalid.write_bytes(settings.backup_vault().encrypt(b"not-a-database", associated_data=BACKUP_AAD))
    destination = tmp_path / "existing" / "northstar.db"
    existing = Database(destination, settings.data_vault(), settings.database_key)
    existing.initialize()
    before = destination.read_bytes()

    with pytest.raises(BackupError, match="integrity"):
        service.restore_to(invalid, destination)

    assert destination.read_bytes() == before
    assert not service.restore_artifact(destination).exists()


def test_restore_refuses_database_with_wal_sidecars(settings: Settings, tmp_path: Path) -> None:
    source = initialized_database(settings)
    service = backup_service(settings, source)
    backup = service.create()
    destination = tmp_path / "existing" / "northstar.db"
    existing = Database(destination, settings.data_vault(), settings.database_key)
    existing.initialize()
    before = destination.read_bytes()
    wal = Path(f"{destination}-wal")
    wal.write_bytes(b"synthetic-sidecar")

    with pytest.raises(BackupError, match="sidecars exist"):
        service.restore_to(backup, destination)

    assert destination.read_bytes() == before
    assert wal.read_bytes() == b"synthetic-sidecar"
    assert not service.restore_artifact(destination).exists()


def test_backup_failure_cleans_temporary_snapshot(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = initialized_database(settings)
    service = backup_service(settings, database)

    def fail_snapshot(_: Path) -> None:
        raise OSError("synthetic snapshot failure")

    monkeypatch.setattr(database, "backup_to", fail_snapshot)
    with pytest.raises(OSError, match="synthetic snapshot failure"):
        service.create()

    assert not list(settings.backup_dir.glob("*.snapshot"))
    assert not list(settings.backup_dir.glob("*.tmp"))
    assert not list(settings.backup_dir.glob("*.nsbak"))


def test_failed_automated_drill_removes_new_backup(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = initialized_database(settings)
    service = backup_service(settings, database)

    def fail_drill(_: Path | None = None, **__: object) -> None:
        raise BackupError("synthetic drill failure")

    monkeypatch.setattr(service, "restore_drill", fail_drill)
    with pytest.raises(BackupError, match="synthetic drill failure"):
        service.create()

    assert not list(settings.backup_dir.glob("*.nsbak"))


def test_retention_keeps_daily_and_monthly_recovery_points(settings: Settings) -> None:
    database = initialized_database(settings)
    service = backup_service(settings, database, BackupRetentionPolicy(daily=2, monthly=2))
    settings.backup_dir.mkdir(exist_ok=True)
    names = [
        "northstar_20260808T120000.000000Z.nsbak",
        "northstar_20260808T080000.000000Z.nsbak",
        "northstar_20260807T120000.000000Z.nsbak",
        "northstar_20260715T120000.000000Z.nsbak",
        "northstar_20260615T120000.000000Z.nsbak",
        "northstar_20260515T120000.000000Z.nsbak",
    ]
    for name in names:
        (settings.backup_dir / name).write_bytes(b"synthetic-encrypted-artifact")

    removed = service.prune()
    remaining = {path.name for path in settings.backup_dir.glob("*.nsbak")}

    assert {path.name for path in removed} == {
        "northstar_20260808T080000.000000Z.nsbak",
        "northstar_20260615T120000.000000Z.nsbak",
        "northstar_20260515T120000.000000Z.nsbak",
    }
    assert remaining == {
        "northstar_20260808T120000.000000Z.nsbak",
        "northstar_20260807T120000.000000Z.nsbak",
        "northstar_20260715T120000.000000Z.nsbak",
    }


def test_isolated_restore_drill_does_not_modify_source_database(settings: Settings) -> None:
    database = initialized_database(settings)
    service = backup_service(settings, database)
    backup = service.create()
    before = settings.database_path.read_bytes()
    before_metadata = settings.database_path.stat()

    result = service.restore_drill(backup)

    after_metadata = settings.database_path.stat()
    assert result.encrypted_records_verified == 1
    assert settings.database_path.read_bytes() == before
    assert after_metadata.st_mtime_ns == before_metadata.st_mtime_ns


def test_backup_can_be_validated_against_an_exact_legacy_schema(settings: Settings) -> None:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    settings.database_path.parent.mkdir(parents=True)
    with closing(database.connect()) as conn:
        conn.executescript(SCHEMA_V1)
        conn.executescript(MIGRATION_2)
        conn.executescript(MIGRATION_3)
        conn.execute(
            "INSERT INTO app_meta(key, value, updated_at) VALUES('schema_version', '3', ?)",
            (utc_now(),),
        )
        conn.commit()
    service = backup_service(settings, database)

    backup = service.create(expected_schema_version=3, prune=False)
    result = service.restore_drill(backup, expected_schema_version=3)

    assert database.schema_version() == 3
    assert result.schema_version == 3
    assert backup.exists()


def test_recovery_commands_verify_and_restore_an_explicit_legacy_schema_backup(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    settings.database_path.parent.mkdir(parents=True)
    with closing(database.connect()) as conn:
        conn.executescript(SCHEMA_V1)
        conn.executescript(MIGRATION_2)
        conn.executescript(MIGRATION_3)
        conn.execute(
            "INSERT INTO app_meta(key, value, updated_at) VALUES('schema_version', '3', ?)",
            (utc_now(),),
        )
        conn.commit()
    service = backup_service(settings, database)
    backup = service.create(expected_schema_version=3, prune=False)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda _: settings))

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_backup_restore.py",
            "--backup",
            str(backup),
            "--expected-schema-version",
            "3",
        ],
    )
    assert verify_backup_restore.main() == 0

    destination = tmp_path / "explicit-recovery" / "northstar.db"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "restore_backup.py",
            "--backup",
            str(backup),
            "--destination",
            str(destination),
            "--expected-schema-version",
            "3",
        ],
    )
    assert restore_backup.main() == 0
    assert database.at_path(destination).schema_version() == 3
