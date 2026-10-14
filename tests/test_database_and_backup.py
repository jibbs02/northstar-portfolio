from __future__ import annotations

from pathlib import Path

from northstar.config import Settings
from northstar.data import Database
from northstar.services import BackupService
from northstar.services.backup import BACKUP_AAD


def test_sensitive_payload_is_encrypted_in_sqlite(settings: Settings) -> None:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    database.initialize()
    database.upsert_secure_record("account", "synthetic-181", {"name": "Synthetic Roth", "balance": 250000})

    raw = settings.database_path.read_bytes()
    assert b"SQLite format 3" not in raw
    assert b"secure_records" not in raw
    assert b"Synthetic Roth" not in raw
    assert b"250000" not in raw
    assert database.get_secure_record("account", "synthetic-181") == {
        "name": "Synthetic Roth",
        "balance": 250000,
    }


def test_encrypted_backup_restores_to_a_valid_database(settings: Settings, tmp_path: Path) -> None:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    database.initialize()
    database.upsert_secure_record("demo", "one", {"safe": True})
    backups = BackupService(database, settings.backup_dir, settings.backup_vault())

    backup_path = backups.create()
    restored_path = tmp_path / "restored" / "northstar.db"
    backups.restore_to(backup_path, restored_path)

    assert backup_path.suffix == ".nsbak"
    assert backup_path.read_bytes().startswith(b"NSV1")
    assert b"SQLite format 3" not in backup_path.read_bytes()
    assert b"secure_records" not in backup_path.read_bytes()
    encrypted_snapshot = settings.backup_vault().decrypt(backup_path.read_bytes(), associated_data=BACKUP_AAD)
    assert b"SQLite format 3" not in encrypted_snapshot
    assert b"secure_records" not in encrypted_snapshot
    assert b'"safe":true' not in encrypted_snapshot
    assert backup_path.stat().st_mode & 0o777 == 0o600
    restored = Database(restored_path, settings.data_vault(), settings.database_key)
    assert restored.health()
    assert restored_path.stat().st_mode & 0o777 == 0o600
    assert restored.get_secure_record("demo", "one") == {"safe": True}
