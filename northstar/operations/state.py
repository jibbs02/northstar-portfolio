from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class OperationalState:
    last_successful_backup: str | None = None
    last_successful_restore_drill: str | None = None
    last_successful_replication: str | None = None
    last_failure_code: str | None = None
    pending_recovery_schema_version: int | None = None


class OperationalStateStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "status.json"

    def read(self) -> OperationalState:
        if not self.path.exists():
            return OperationalState()
        metadata = self.path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError("Operational status file type is unsafe")
        if metadata.st_mode & 0o077 or metadata.st_uid != os.getuid():
            raise ValueError("Operational status file permissions are unsafe")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        allowed = {field.name for field in OperationalState.__dataclass_fields__.values()}
        if set(payload) - allowed:
            raise ValueError("Operational status contains unsupported metadata")
        return OperationalState(**payload)

    def success(self, event: str, *, when: datetime | None = None) -> OperationalState:
        timestamp = (when or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
        current = self.read()
        fields = {
            "backup": "last_successful_backup",
            "restore_drill": "last_successful_restore_drill",
            "replication": "last_successful_replication",
        }
        field = fields.get(event)
        if field is None:
            raise ValueError("Unsupported operational event")
        updated = replace(current, **{field: timestamp, "last_failure_code": None})
        self._write(updated)
        return updated

    def failure(self, code: str) -> OperationalState:
        if not code or len(code) > 80 or not code.replace("_", "").isalnum():
            raise ValueError("Operational failure code is invalid")
        updated = replace(self.read(), last_failure_code=code)
        self._write(updated)
        return updated

    def recovery_schema(self, version: int | None) -> OperationalState:
        if version is not None and not 1 <= version <= 999:
            raise ValueError("Recovery schema version is invalid")
        updated = replace(self.read(), pending_recovery_schema_version=version)
        self._write(updated)
        return updated

    def _write(self, state: OperationalState) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = self.directory.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_mode & 0o077
            or metadata.st_uid != os.getuid()
        ):
            raise ValueError("Operational status directory permissions are unsafe")
        descriptor, name = tempfile.mkstemp(prefix=".status.", suffix=".tmp", dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(asdict(state), handle, separators=(",", ":"), sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
            self.path.chmod(0o600)
            _fsync_directory(self.directory)
        finally:
            temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
