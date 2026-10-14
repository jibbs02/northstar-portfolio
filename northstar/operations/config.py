from __future__ import annotations

import base64
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from northstar.config import (
    PRODUCTION_HOSTNAME,
    validate_production_hostname,
    validate_schwab_client_value,
    validate_schwab_redirect_uri,
)

KEY_NAMES = (
    "NORTHSTAR_DATABASE_KEY",
    "NORTHSTAR_DATA_KEY",
    "NORTHSTAR_BACKUP_KEY",
    "NORTHSTAR_BLIND_INDEX_KEY",
)
SCHWAB_ENV_NAMES = (
    "SCHWAB_CLIENT_ID",
    "SCHWAB_CLIENT_SECRET",
    "SCHWAB_REDIRECT_URI",
)
SCHWAB_ORDER_THROTTLE_VERIFICATION = "NORTHSTAR_SCHWAB_ORDER_THROTTLE_VERIFIED"


@dataclass(frozen=True)
class OperationalConfig:
    repository_root: Path
    runtime_root: Path
    operations_dir: Path
    replication_dir: Path | None
    secrets_path: Path
    container_name: str = "northstar-local"
    expected_container_user: str = "10001:10001"
    bind_address: str = "127.0.0.1"
    minimum_free_bytes: int = 512 * 1024 * 1024

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        repository_root: Path,
        database_path: Path,
        backup_dir: Path,
        secrets_path: Path,
    ) -> OperationalConfig:
        runtime_value = environment.get("NORTHSTAR_RUNTIME_ROOT")
        runtime_root = Path(runtime_value).expanduser() if runtime_value else database_path.parent.parent
        operations_value = environment.get("NORTHSTAR_OPERATIONS_DIR")
        operations_dir = (
            Path(operations_value).expanduser() if operations_value else runtime_root / "operations"
        )
        replication_value = environment.get("NORTHSTAR_REPLICATION_DIR", "").strip()
        minimum_free = int(environment.get("NORTHSTAR_MINIMUM_FREE_BYTES", str(512 * 1024 * 1024)))
        if minimum_free < 1:
            raise ValueError("Minimum free bytes must be positive")
        bind_address = environment.get("NORTHSTAR_BIND_ADDRESS", "127.0.0.1").strip()
        if bind_address != "127.0.0.1":
            raise ValueError("Production bind address must be explicit IPv4 loopback")
        return cls(
            repository_root.resolve(),
            _absolute(runtime_root),
            _absolute(operations_dir),
            _absolute(Path(replication_value).expanduser()) if replication_value else None,
            _absolute(secrets_path),
            environment.get("NORTHSTAR_CONTAINER_NAME", "northstar-local").strip(),
            environment.get("NORTHSTAR_EXPECTED_CONTAINER_USER", "10001:10001").strip(),
            bind_address,
            minimum_free,
        )


def load_secrets_environment(path: Path) -> dict[str, str]:
    """Read a strict owner-only KEY=VALUE file without shell evaluation."""
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError("Secrets file must be a regular file")
    if metadata.st_mode & 0o077 or not metadata.st_mode & stat.S_IRUSR:
        raise ValueError("Secrets file permissions are unsafe")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ValueError("Secrets file contains an invalid line")
        name, value = stripped.split("=", 1)
        if not name or not name.replace("_", "").isalnum() or not name[0].isalpha():
            raise ValueError("Secrets file contains an invalid variable name")
        if name in values:
            raise ValueError("Secrets file contains a duplicate variable")
        values[name] = value
    return values


def apply_environment(values: Mapping[str, str]) -> None:
    for name, value in values.items():
        if name.startswith("NORTHSTAR_") or name in SCHWAB_ENV_NAMES:
            os.environ[name] = value


def validate_schwab_environment(values: Mapping[str, str]) -> None:
    enabled = values.get("NORTHSTAR_SCHWAB_ENABLED", "0")
    if enabled not in {"0", "1"}:
        raise ValueError("The Schwab enable flag is malformed")
    throttle_verified = values.get(SCHWAB_ORDER_THROTTLE_VERIFICATION, "0")
    if throttle_verified not in {"0", "1"}:
        raise ValueError("The Schwab zero order-throttle verification is malformed")
    if enabled == "0":
        if throttle_verified != "0":
            raise ValueError("A disabled Schwab integration cannot retain throttle verification")
        return
    if throttle_verified != "1":
        raise ValueError("The Schwab zero order-throttle verification is missing")
    validate_schwab_client_value(values.get("SCHWAB_CLIENT_ID"), "client ID", 512)
    validate_schwab_client_value(values.get("SCHWAB_CLIENT_SECRET"), "client secret", 2_048)
    validate_schwab_redirect_uri(
        values.get("SCHWAB_REDIRECT_URI"),
        environment="production",
        production_origin="https://" + validate_production_hostname(
            values.get("NORTHSTAR_PRODUCTION_HOSTNAME", PRODUCTION_HOSTNAME)
        ),
    )


def validate_key_environment(values: Mapping[str, str]) -> None:
    decoded: list[bytes] = []
    for name in KEY_NAMES:
        value = values.get(name, "")
        if not value:
            raise ValueError("A required encryption key is missing")
        try:
            raw = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("An encryption key has an invalid encoding") from exc
        if len(raw) != 32:
            raise ValueError("An encryption key has an invalid length")
        decoded.append(raw)
    if len(set(decoded)) != len(decoded):
        raise ValueError("Encryption keys must be independent")
    session_key = values.get("NORTHSTAR_SESSION_KEY", "")
    if len(session_key) < 32:
        raise ValueError("The session key is missing or malformed")
    try:
        version = int(values.get("NORTHSTAR_KEY_VERSION", ""))
    except ValueError as exc:
        raise ValueError("The key version is malformed") from exc
    if version < 1:
        raise ValueError("The key version is malformed")


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))
