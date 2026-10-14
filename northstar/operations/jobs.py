from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from northstar.operations.replication import ReplicationResult, ReplicationService
from northstar.operations.state import OperationalStateStore
from northstar.services import BackupService, RestoreDrillResult


class JobLockedError(RuntimeError):
    """Raised when an operational job is already running."""


class ExclusiveJobLock:
    def __init__(self, directory: Path, name: str) -> None:
        self.directory = directory
        self.path = directory / f"{name}.lock"
        self.descriptor: int | None = None

    def __enter__(self) -> ExclusiveJobLock:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _require_private_directory(self.directory)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise JobLockedError("An operational job is already running") from exc
        self.descriptor = descriptor
        return self

    def __exit__(self, *_: object) -> None:
        if self.descriptor is not None:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = None


@dataclass(frozen=True)
class BackupJobResult:
    backup_count: int
    replication: ReplicationResult | None
    backup_path: Path


class ScheduledBackupJob:
    def __init__(
        self,
        backup_service: BackupService,
        state: OperationalStateStore,
        operations_dir: Path,
        *,
        replication: ReplicationService | None = None,
    ) -> None:
        self.backup_service = backup_service
        self.state = state
        self.operations_dir = operations_dir
        self.replication = replication

    def run(self, *, expected_schema_version: int | None = None, prune: bool = True) -> BackupJobResult:
        try:
            with ExclusiveJobLock(self.operations_dir, "backup"):
                create_options: dict[str, object] = {"prune": prune}
                if expected_schema_version is not None:
                    create_options["expected_schema_version"] = expected_schema_version
                backup_path = self.backup_service.create(**create_options)
                self.state.success("backup")
                self.state.success("restore_drill")
                replication_result = None
                if self.replication is not None:
                    replication_result = self.replication.run()
                    self.state.success("replication")
                backup_count = len(
                    [
                        path
                        for path in self.backup_service.backup_dir.glob("northstar_*.nsbak")
                        if path.is_file() and not path.is_symlink()
                    ]
                )
                return BackupJobResult(backup_count, replication_result, backup_path)
        except JobLockedError:
            self.state.failure("backup_already_running")
            raise
        except Exception:
            self.state.failure("backup_job_failed")
            raise


class MonthlyRecoveryJob:
    def __init__(
        self,
        backup_service: BackupService,
        state: OperationalStateStore,
        operations_dir: Path,
    ) -> None:
        self.backup_service = backup_service
        self.state = state
        self.operations_dir = operations_dir

    def run(self) -> RestoreDrillResult:
        try:
            with ExclusiveJobLock(self.operations_dir, "recovery"):
                selected = self.backup_service.latest()
                if selected is None:
                    raise ValueError("No validated backup is available")
                self.operations_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tempfile.TemporaryDirectory(
                    prefix="recovery-drill-", dir=self.operations_dir
                ) as directory:
                    isolated = Path(directory)
                    isolated.chmod(0o700)
                    copied = isolated / "validated.nsbak"
                    _copy_owner_only(selected, copied)
                    if _hash_file(selected) != _hash_file(copied):
                        raise ValueError("Isolated backup copy failed verification")
                    result = self.backup_service.restore_drill(copied)
                self.state.success("restore_drill")
                return result
        except JobLockedError:
            self.state.failure("recovery_already_running")
            raise
        except Exception:
            self.state.failure("recovery_drill_failed")
            raise


def _copy_owner_only(source: Path, destination: Path) -> None:
    metadata = source.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_mode & 0o077
        or metadata.st_uid != os.getuid()
    ):
        raise ValueError("Validated backup has an unsafe type")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as target_handle:
            while chunk := source_handle.read(1024 * 1024):
                target_handle.write(chunk)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        destination.chmod(0o600)
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _hash_file(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.digest()


def _require_private_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_mode & 0o077
        or metadata.st_uid != os.getuid()
    ):
        raise ValueError("Operational job directory permissions are unsafe")
