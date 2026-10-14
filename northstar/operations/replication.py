from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from northstar.crypto import Vault
from northstar.services.backup import BACKUP_AAD, BACKUP_NAME


class ReplicationError(RuntimeError):
    """Raised when encrypted backup replication cannot complete safely."""


@dataclass(frozen=True)
class ReplicationResult:
    source_count: int
    copied_count: int
    verified_count: int
    dry_run: bool


class ReplicationService:
    def __init__(
        self,
        source_dir: Path,
        destination_dir: Path,
        backup_vault: Vault,
        *,
        repository_root: Path,
        runtime_root: Path,
    ) -> None:
        self.source_dir = source_dir
        self.destination_dir = destination_dir
        self.backup_vault = backup_vault
        self.repository_root = repository_root.resolve()
        self.runtime_root = runtime_root.resolve()

    def run(self, *, dry_run: bool = False, verify_only: bool = False) -> ReplicationResult:
        self._validate_source()
        self._validate_destination()
        sources = self._sources()
        if not sources:
            raise ReplicationError("No finalized encrypted backups are available")
        self._remove_partial_artifacts()
        copied = 0
        verified = 0
        for source in sources:
            self._authenticate(source)
            destination = self.destination_dir / source.name
            if destination.exists() or destination.is_symlink():
                self._verify_pair(source, destination)
                verified += 1
                continue
            if verify_only:
                raise ReplicationError("A finalized backup is missing from the destination")
            if dry_run:
                continue
            self._copy_atomic(source, destination)
            self._verify_pair(source, destination)
            copied += 1
            verified += 1
        return ReplicationResult(len(sources), copied, verified, dry_run)

    def _validate_source(self) -> None:
        try:
            metadata = self.source_dir.lstat()
        except FileNotFoundError as exc:
            raise ReplicationError("Backup source is unavailable") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_mode & 0o077
            or metadata.st_uid != os.getuid()
        ):
            raise ReplicationError("Backup source permissions are unsafe")

    def _validate_destination(self) -> None:
        destination = self.destination_dir.resolve()
        if destination.is_relative_to(self.repository_root) or destination.is_relative_to(self.runtime_root):
            raise ReplicationError("Replication destination is inside a prohibited boundary")
        try:
            metadata = self.destination_dir.lstat()
        except FileNotFoundError as exc:
            raise ReplicationError("Replication destination is unavailable") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_mode & 0o077
            or metadata.st_uid != os.getuid()
        ):
            raise ReplicationError("Replication destination permissions are unsafe")

    def _sources(self) -> list[Path]:
        sources: list[Path] = []
        for path in self.source_dir.iterdir():
            if BACKUP_NAME.fullmatch(path.name) is None:
                continue
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_mode & 0o077
                or metadata.st_uid != os.getuid()
            ):
                raise ReplicationError("A source backup has unsafe permissions")
            sources.append(path)
        return sorted(sources, key=lambda path: path.name)

    def _authenticate(self, path: Path) -> None:
        try:
            self.backup_vault.decrypt(path.read_bytes(), associated_data=BACKUP_AAD)
        except (OSError, ValueError) as exc:
            raise ReplicationError("A finalized backup failed authentication") from exc

    def _copy_atomic(self, source: Path, destination: Path) -> None:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".partial", dir=self.destination_dir
        )
        temporary = Path(name)
        source_hash = hashlib.sha256()
        destination_hash = hashlib.sha256()
        source_size = 0
        destination_size = 0
        try:
            with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as target_handle:
                while chunk := source_handle.read(1024 * 1024):
                    source_hash.update(chunk)
                    source_size += len(chunk)
                    target_handle.write(chunk)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            temporary.chmod(0o600)
            with temporary.open("rb") as target_handle:
                while chunk := target_handle.read(1024 * 1024):
                    destination_hash.update(chunk)
                    destination_size += len(chunk)
            if source_size != destination_size or source_hash.digest() != destination_hash.digest():
                raise ReplicationError("Replicated backup failed copy verification")
            os.replace(temporary, destination)
            destination.chmod(0o600)
            _fsync_directory(self.destination_dir)
        finally:
            temporary.unlink(missing_ok=True)

    def _verify_pair(self, source: Path, destination: Path) -> None:
        try:
            metadata = destination.lstat()
        except FileNotFoundError as exc:
            raise ReplicationError("Replicated backup is unavailable") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_mode & 0o077
            or metadata.st_uid != os.getuid()
        ):
            raise ReplicationError("Replicated backup permissions are unsafe")
        if source.stat().st_size != destination.stat().st_size:
            raise ReplicationError("Replicated backup size does not match")
        if _hash_file(source) != _hash_file(destination):
            raise ReplicationError("Replicated backup hash does not match")
        self._authenticate(destination)

    def _remove_partial_artifacts(self) -> None:
        for path in self.destination_dir.glob(".northstar_*.nsbak.*.partial"):
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ReplicationError("A partial replication artifact has an unsafe type")
            path.unlink()
        _fsync_directory(self.destination_dir)


def _hash_file(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.digest()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
