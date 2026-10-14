from __future__ import annotations

import os
import re
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from northstar.crypto import Vault
from northstar.data import Database
from northstar.data.database import LATEST_SCHEMA_VERSION

BACKUP_AAD = b"northstar-encrypted-backup-v1"
BACKUP_NAME = re.compile(r"^northstar_(\d{8})T(\d{6})(?:\.(\d{6}))?Z\.nsbak$")


class BackupError(RuntimeError):
    """Raised when a backup or restore cannot be completed safely."""


@dataclass(frozen=True)
class BackupRetentionPolicy:
    daily: int = 14
    monthly: int = 12

    def __post_init__(self) -> None:
        if self.daily < 0 or self.monthly < 0:
            raise ValueError("Backup retention values cannot be negative")
        if self.daily == 0 and self.monthly == 0:
            raise ValueError("At least one backup retention window must be enabled")


@dataclass(frozen=True)
class RestoreDrillResult:
    backup_path: Path
    encrypted_records_verified: int
    schema_version: int


class BackupService:
    def __init__(
        self,
        database: Database,
        backup_dir: Path,
        backup_vault: Vault,
        retention: BackupRetentionPolicy | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.backup_dir = backup_dir
        self.backup_vault = backup_vault
        self.retention = retention or BackupRetentionPolicy()
        self.clock = clock or (lambda: datetime.now(UTC))

    def create(
        self,
        *,
        expected_schema_version: int = LATEST_SCHEMA_VERSION,
        prune: bool = True,
    ) -> Path:
        if not self.database.health():
            raise BackupError("Source database failed health validation")
        self.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.backup_dir.chmod(0o700)
        now = self.clock().astimezone(UTC)
        stamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
        destination = self.backup_dir / f"northstar_{stamp}.nsbak"
        snapshot = self._temporary_path(self.backup_dir, ".snapshot")
        try:
            self.database.backup_to(snapshot)
            encrypted_database = snapshot.read_bytes()
            ciphertext = self.backup_vault.encrypt(encrypted_database, associated_data=BACKUP_AAD)
            self._atomic_write(destination, ciphertext)
        finally:
            snapshot.unlink(missing_ok=True)

        try:
            self.restore_drill(destination, expected_schema_version=expected_schema_version)
        except Exception:
            destination.unlink(missing_ok=True)
            _fsync_directory(self.backup_dir)
            raise
        if prune:
            self.prune()
        return destination

    def restore_to(
        self,
        backup_path: Path,
        destination: Path,
        *,
        expected_schema_version: int = LATEST_SCHEMA_VERSION,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.parent.chmod(0o700)
        temporary = self.restore_artifact(destination)
        self.remove_stale_restore(destination)
        sidecars = [path for path in _database_sidecars(destination) if os.path.lexists(path)]
        if sidecars:
            names = ", ".join(path.name for path in sidecars)
            raise BackupError(
                f"Refusing to restore while database sidecars exist ({names}); stop the app and "
                "preserve the database set before recovery"
            )
        encrypted_database = self.backup_vault.decrypt(backup_path.read_bytes(), associated_data=BACKUP_AAD)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encrypted_database)
                handle.flush()
                os.fsync(handle.fileno())
            restored = self.database.at_path(temporary)
            if not restored.health():
                raise BackupError("Restored SQLCipher database failed integrity validation")
            restored.verify_encrypted_records()
            restored.verify_critical_invariants(expected_schema_version)
            temporary.chmod(0o600)
            _fsync_file(temporary)
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def restore_drill(
        self,
        backup_path: Path | None = None,
        *,
        expected_schema_version: int = LATEST_SCHEMA_VERSION,
    ) -> RestoreDrillResult:
        selected = backup_path or self.latest()
        if selected is None:
            raise BackupError("No encrypted backup is available for a restore drill")
        with tempfile.TemporaryDirectory(prefix="northstar-restore-drill-") as directory:
            destination = Path(directory) / "northstar.db"
            if destination.resolve() == self.database.path.resolve():
                raise BackupError("Restore drill destination cannot be the production database")
            self.restore_to(
                selected,
                destination,
                expected_schema_version=expected_schema_version,
            )
            restored = self.database.at_path(destination)
            if not restored.health():
                raise BackupError("Isolated restore drill failed database health validation")
            verified = restored.verify_encrypted_records()
            schema_version = restored.verify_critical_invariants(expected_schema_version)
        return RestoreDrillResult(selected, verified, schema_version)

    def prune(self) -> list[Path]:
        candidates: list[tuple[Path, datetime]] = []
        for path in self.backup_dir.glob("northstar_*.nsbak"):
            if path.is_symlink() or not path.is_file():
                continue
            timestamp = _backup_timestamp(path)
            if timestamp is not None:
                candidates.append((path, timestamp))
        candidates.sort(key=lambda item: item[1], reverse=True)

        keep: set[Path] = set()
        daily_points: set[tuple[int, int, int]] = set()
        monthly_points: set[tuple[int, int]] = set()
        for path, timestamp in candidates:
            day = (timestamp.year, timestamp.month, timestamp.day)
            month = (timestamp.year, timestamp.month)
            if day not in daily_points and len(daily_points) < self.retention.daily:
                daily_points.add(day)
                keep.add(path)
            if month not in monthly_points and len(monthly_points) < self.retention.monthly:
                monthly_points.add(month)
                keep.add(path)

        removed: list[Path] = []
        for path, _ in candidates:
            if path not in keep:
                path.unlink()
                removed.append(path)
        if removed:
            _fsync_directory(self.backup_dir)
        return removed

    def latest(self) -> Path | None:
        candidates = [
            (path, timestamp)
            for path in self.backup_dir.glob("northstar_*.nsbak")
            if not path.is_symlink() and path.is_file()
            if (timestamp := _backup_timestamp(path)) is not None
        ]
        return max(candidates, key=lambda item: item[1])[0] if candidates else None

    def creation_time(self, backup_path: Path) -> datetime:
        """Return the canonical creation time encoded in a finalized backup name."""
        try:
            metadata = backup_path.lstat()
        except FileNotFoundError as exc:
            raise BackupError("Encrypted backup is unavailable") from exc
        if (
            backup_path.parent.resolve() != self.backup_dir.resolve()
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
        ):
            raise BackupError("Encrypted backup path is invalid")
        created_at = _backup_timestamp(backup_path)
        if created_at is None:
            raise BackupError("Encrypted backup name is invalid")
        return created_at

    @staticmethod
    def restore_artifact(destination: Path) -> Path:
        return destination.with_suffix(destination.suffix + ".restore")

    def remove_stale_restore(self, destination: Path) -> bool:
        artifact = self.restore_artifact(destination)
        if not os.path.lexists(artifact):
            return False
        metadata = artifact.lstat()
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
            raise BackupError(f"Refusing to remove unexpected restore artifact type: {artifact}")
        artifact.unlink()
        _fsync_directory(artifact.parent)
        return True

    @staticmethod
    def _temporary_path(directory: Path, suffix: str) -> Path:
        descriptor, name = tempfile.mkstemp(prefix=".northstar-", suffix=suffix, dir=directory)
        os.close(descriptor)
        path = Path(name)
        path.chmod(0o600)
        return path

    @staticmethod
    def _atomic_write(destination: Path, payload: bytes) -> None:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)


def _backup_timestamp(path: Path) -> datetime | None:
    match = BACKUP_NAME.fullmatch(path.name)
    if match is None:
        return None
    date, time, fractional = match.groups()
    microseconds = fractional or "000000"
    return datetime.strptime(f"{date}{time}{microseconds}", "%Y%m%d%H%M%S%f").replace(tzinfo=UTC)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _database_sidecars(database: Path) -> tuple[Path, Path]:
    return (Path(f"{database}-wal"), Path(f"{database}-shm"))
