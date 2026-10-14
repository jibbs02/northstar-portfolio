from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from northstar.config import Settings
from northstar.operations.config import OperationalConfig
from northstar.services.backup import BACKUP_NAME

SQLITE_HEADER = b"SQLite format 3\x00"
MINIMUM_COMPOSE_VERSION = (2, 30, 0)


@dataclass(frozen=True)
class PreflightCheck:
    code: str
    passed: bool


@dataclass(frozen=True)
class PreflightReport:
    checks: tuple[PreflightCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "status": "pass" if self.passed else "fail",
            "checks": {check.code: "pass" if check.passed else "fail" for check in self.checks},
        }


class SystemProbe:
    def filevault_status(self) -> str:
        result = subprocess.run(  # noqa: S603
            ["/usr/bin/fdesetup", "status"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout if result.returncode == 0 else ""

    def docker_info(self) -> bool:
        docker = shutil.which("docker")
        if docker is None:
            return False
        result = subprocess.run(  # noqa: S603
            [docker, "info", "--format", "{{json .ServerVersion}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    def docker_compose_version(self) -> str:
        docker = shutil.which("docker")
        if docker is None:
            return ""
        result = subprocess.run(  # noqa: S603
            [docker, "compose", "version", "--short"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return result.stdout if result.returncode == 0 else ""

    def docker_compose_config(
        self,
        repository_root: Path,
        *,
        secrets_path: Path,
        data_dir: Path,
        backup_dir: Path,
    ) -> bool:
        docker = shutil.which("docker")
        if docker is None:
            return False
        environment = os.environ.copy()
        environment.update(
            {
                "NORTHSTAR_SECRET_FILE": str(secrets_path),
                "NORTHSTAR_DATA_DIR": str(data_dir),
                "NORTHSTAR_BACKUP_DIR": str(backup_dir),
                # Compose requires a build revision even though this read-only check never builds.
                "NORTHSTAR_BUILD_COMMIT": "0" * 40,
            }
        )
        result = subprocess.run(  # noqa: S603
            [docker, "compose", "config", "--quiet"],
            cwd=repository_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0

    def container_inspect(self, container_name: str) -> dict[str, Any] | None:
        docker = shutil.which("docker")
        if docker is None:
            return None
        result = subprocess.run(  # noqa: S603
            [docker, "inspect", container_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return payload[0] if isinstance(payload, list) and len(payload) == 1 else None

    def free_bytes(self, path: Path) -> int:
        return shutil.disk_usage(path).free


def parse_filevault_enabled(output: str) -> bool:
    normalized = " ".join(output.strip().lower().split())
    return normalized in {"filevault is on.", "filevault is on"}


def parse_compose_version(output: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(
        r"v?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?",
        output.strip(),
    )
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


class ProductionPreflight:
    def __init__(
        self,
        settings: Settings,
        operational: OperationalConfig,
        *,
        probe: SystemProbe | None = None,
    ) -> None:
        self.settings = settings
        self.operational = operational
        self.probe = probe or SystemProbe()

    def run(self) -> PreflightReport:
        checks: list[PreflightCheck] = []
        self._check(checks, "filevault", lambda: parse_filevault_enabled(self.probe.filevault_status()))
        self._check(checks, "production_config", self._production_config_safe)
        self._check(checks, "key_material", self._key_material_safe)
        self._check(checks, "runtime_paths", self._paths_safe)
        self._check(checks, "runtime_permissions", self._permissions_safe)
        self._check(checks, "encrypted_storage", self._no_plaintext_sqlite)
        self._check(checks, "disk_space", self._disk_space_safe)
        self._check(checks, "docker_daemon", self.probe.docker_info)
        self._check(checks, "compose_version", self._compose_version_safe)
        self._check(checks, "compose_config", self._compose_config_safe)
        inspection: dict[str, Any] | None = None
        try:
            inspection = self.probe.container_inspect(self.operational.container_name)
        except (OSError, subprocess.SubprocessError, ValueError):
            inspection = None
        self._check(checks, "container_identity", lambda: self._container_identity_safe(inspection))
        self._check(checks, "loopback_publication", lambda: self._publication_safe(inspection))
        return PreflightReport(tuple(checks))

    def _compose_version_safe(self) -> bool:
        version = parse_compose_version(self.probe.docker_compose_version())
        return version is not None and version >= MINIMUM_COMPOSE_VERSION

    def _compose_config_safe(self) -> bool:
        return self.probe.docker_compose_config(
            self.operational.repository_root,
            secrets_path=self.operational.secrets_path,
            data_dir=self.settings.database_path.parent,
            backup_dir=self.settings.backup_dir,
        )

    @staticmethod
    def _check(checks: list[PreflightCheck], code: str, operation: Callable[[], bool]) -> None:
        try:
            passed = bool(operation())
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            passed = False
        checks.append(PreflightCheck(code, passed))

    def _key_material_safe(self) -> bool:
        keys = (
            self.settings.database_key,
            self.settings.data_key,
            self.settings.backup_key,
            self.settings.blind_index_key,
        )
        return (
            all(len(value) == 32 for value in keys)
            and len(set(keys)) == len(keys)
            and len(self.settings.session_key) >= 32
            and self.settings.key_version >= 1
        )

    def _production_config_safe(self) -> bool:
        try:
            expected_uid, expected_gid = (
                int(part) for part in self.operational.expected_container_user.split(":", 1)
            )
        except (TypeError, ValueError):
            return False
        return (
            self.settings.environment == "production"
            and self.settings.auth_mode == "cloudflare"
            and self.settings.secure_cookies
            and expected_uid > 0
            and expected_gid > 0
            and bool(self.operational.container_name)
        )

    def _paths_safe(self) -> bool:
        repository = self.operational.repository_root.resolve()
        paths = (
            self.operational.runtime_root,
            self.operational.operations_dir,
            self.settings.database_path.parent,
            self.settings.backup_dir,
            self.operational.secrets_path,
        )
        if any(path.resolve().is_relative_to(repository) for path in paths):
            return False
        if self.operational.replication_dir is not None:
            destination = self.operational.replication_dir.resolve()
            if destination.is_relative_to(repository) or destination.is_relative_to(
                self.operational.runtime_root.resolve()
            ):
                return False
        return True

    def _permissions_safe(self) -> bool:
        directories = {
            self.operational.runtime_root,
            self.settings.database_path.parent,
            self.settings.backup_dir,
            self.operational.operations_dir,
        }
        if self.operational.replication_dir is not None:
            directories.add(self.operational.replication_dir)
        if not all(_private_directory(path) for path in directories):
            return False
        if not _private_file(self.operational.secrets_path, require_owner_read=True):
            return False
        artifacts = [
            self.settings.database_path,
            Path(f"{self.settings.database_path}-wal"),
            Path(f"{self.settings.database_path}-shm"),
            self.settings.database_path.with_suffix(self.settings.database_path.suffix + ".restore"),
        ]
        artifacts.extend(self.settings.backup_dir.glob("*"))
        for path in artifacts:
            if not os.path.lexists(path):
                continue
            if not _private_file(path):
                return False
            if path.parent == self.settings.backup_dir and BACKUP_NAME.fullmatch(path.name) is None:
                return False
        return True

    def _no_plaintext_sqlite(self) -> bool:
        roots = {self.operational.runtime_root, self.settings.backup_dir}
        for root in roots:
            for path in root.rglob("*"):
                if path.is_symlink():
                    return False
                if not path.is_file():
                    continue
                with path.open("rb") as handle:
                    if handle.read(len(SQLITE_HEADER)) == SQLITE_HEADER:
                        return False
        return True

    def _disk_space_safe(self) -> bool:
        database_bytes = sum(
            path.stat().st_size
            for path in (
                self.settings.database_path,
                Path(f"{self.settings.database_path}-wal"),
                Path(f"{self.settings.database_path}-shm"),
            )
            if path.exists() and path.is_file() and not path.is_symlink()
        )
        backup_bytes = max(
            (
                path.stat().st_size
                for path in self.settings.backup_dir.glob("northstar_*.nsbak")
                if path.is_file() and not path.is_symlink()
            ),
            default=0,
        )
        required = max(
            self.operational.minimum_free_bytes,
            database_bytes * 4 + backup_bytes * 2,
        )
        paths = {self.settings.database_path.parent, self.settings.backup_dir}
        if self.operational.replication_dir is not None:
            paths.add(self.operational.replication_dir)
        return all(self.probe.free_bytes(path) >= required for path in paths)

    def _container_identity_safe(self, inspection: dict[str, Any] | None) -> bool:
        if inspection is None:
            return False
        config = inspection.get("Config", {})
        host = inspection.get("HostConfig", {})
        state = inspection.get("State", {})
        health = state.get("Health", {}).get("Status")
        return (
            config.get("User") == self.operational.expected_container_user
            and state.get("Running") is True
            and health == "healthy"
            and host.get("ReadonlyRootfs") is True
            and set(host.get("CapDrop") or []) == {"ALL"}
            and "no-new-privileges:true" in (host.get("SecurityOpt") or [])
        )

    def _publication_safe(self, inspection: dict[str, Any] | None) -> bool:
        if inspection is None:
            return False
        bindings = inspection.get("HostConfig", {}).get("PortBindings") or {}
        published = [binding for values in bindings.values() for binding in (values or [])]
        return bool(published) and all(
            binding.get("HostIp") == self.operational.bind_address
            and str(binding.get("HostPort", "")).isdigit()
            for binding in published
        )


def _private_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_mode & 0o077 == 0
        and metadata.st_uid == os.getuid()
    )


def _private_file(path: Path, *, require_owner_read: bool = False) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    safe = (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_mode & 0o077 == 0
        and metadata.st_uid == os.getuid()
    )
    return safe and (not require_owner_read or bool(metadata.st_mode & stat.S_IRUSR))
