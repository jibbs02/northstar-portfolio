#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import getpass
import os
import secrets
import stat
import subprocess
from pathlib import Path

from northstar.config import (
    normalize_allowed_email,
    validate_cloudflare_audience,
    validate_cloudflare_issuer,
)
from northstar.operations.config import (
    load_secrets_environment,
    validate_key_environment,
    validate_schwab_environment,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FINAL_RUNTIME_ROOT = Path.home() / ".northstar"
RECOVERY_DESTINATION = Path("/Volumes/NorthstarRecovery/northstar-backups")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or validate the final Mac mini runtime.")
    parser.add_argument("--root", type=Path, default=Path("~/.northstar"))
    args = parser.parse_args()
    try:
        _require_mac_mini()
        root = args.root.expanduser().absolute()
        if root != FINAL_RUNTIME_ROOT:
            raise ValueError("Final production runtime path must be ~/.northstar")
        _validate_root_boundary(root)
        secrets_path = root / "secrets.env"
        if os.path.lexists(secrets_path):
            _validate_existing(root, secrets_path)
            print("production_runtime: existing_valid")
            return 0
        issuer = validate_cloudflare_issuer(getpass.getpass("Cloudflare team issuer (hidden): "))
        audience = validate_cloudflare_audience(getpass.getpass("Cloudflare application audience (hidden): "))
        allowed_email = normalize_allowed_email(getpass.getpass("Allowed identity email (hidden): "))
        _create(root, secrets_path, issuer, audience, allowed_email)
    except Exception:
        print("production_runtime: fail")
        return 1
    print("production_runtime: created")
    print("production_runtime: password_manager_copy_required")
    return 0


def _require_mac_mini() -> None:
    result = subprocess.run(  # noqa: S603
        ["/usr/sbin/system_profiler", "SPHardwareDataType"],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0 or "Model Name: Mac mini" not in result.stdout:
        raise ValueError("Final production runtime must be created on the Mac mini")


def _create(
    root: Path,
    secrets_path: Path,
    issuer: str,
    audience: str,
    allowed_email: str,
) -> None:
    _validate_root_boundary(root)
    if secrets_path != root / "secrets.env":
        raise ValueError("Production secrets path is invalid")
    data = root / "data"
    backups = root / "backups"
    operations = root / "operations"
    for directory in (root, data, backups, operations):
        if os.path.lexists(directory):
            if not _private_directory(directory):
                raise ValueError("Production runtime directory is unsafe")
        else:
            directory.mkdir(parents=True, exist_ok=False, mode=0o700)
            directory.chmod(0o700)
    encoded_keys = [_key() for _ in range(4)]
    if len(set(encoded_keys)) != 4:
        raise ValueError("Generated encryption keys are not independent")
    values = {
        "NORTHSTAR_ENV": "production",
        "NORTHSTAR_AUTH_MODE": "cloudflare",
        "NORTHSTAR_DATABASE_PATH": str(data / "northstar.db"),
        "NORTHSTAR_BACKUP_DIR": str(backups),
        "NORTHSTAR_RUNTIME_ROOT": str(root),
        "NORTHSTAR_OPERATIONS_DIR": str(operations),
        "NORTHSTAR_SECRETS_PATH": str(secrets_path),
        "NORTHSTAR_DATABASE_KEY": encoded_keys[0],
        "NORTHSTAR_DATA_KEY": encoded_keys[1],
        "NORTHSTAR_BACKUP_KEY": encoded_keys[2],
        "NORTHSTAR_BLIND_INDEX_KEY": encoded_keys[3],
        "NORTHSTAR_SESSION_KEY": secrets.token_urlsafe(48),
        "NORTHSTAR_KEY_VERSION": "1",
        "NORTHSTAR_BACKUP_DAILY_RETENTION": "14",
        "NORTHSTAR_BACKUP_MONTHLY_RETENTION": "12",
        "NORTHSTAR_CF_ISSUER": issuer,
        "NORTHSTAR_CF_AUDIENCE": audience,
        "NORTHSTAR_ALLOWED_EMAIL": allowed_email,
        "NORTHSTAR_SECURE_COOKIES": "1",
        "NORTHSTAR_BIND_ADDRESS": "127.0.0.1",
        "NORTHSTAR_CONTAINER_NAME": "northstar-local",
        "NORTHSTAR_EXPECTED_CONTAINER_USER": "10001:10001",
        "NORTHSTAR_MINIMUM_FREE_BYTES": str(512 * 1024 * 1024),
        "NORTHSTAR_SCHWAB_ENABLED": "0",
        "NORTHSTAR_SCHWAB_ORDER_THROTTLE_VERIFIED": "0",
        "SCHWAB_CLIENT_ID": "",
        "SCHWAB_CLIENT_SECRET": "",
        "SCHWAB_REDIRECT_URI": "",
    }
    descriptor = os.open(secrets_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for name, value in values.items():
                handle.write(f"{name}={value}\n")
            handle.flush()
            os.fsync(handle.fileno())
        secrets_path.chmod(0o600)
        _fsync_directory(root)
        _validate_existing(root, secrets_path)
    except Exception:
        secrets_path.unlink(missing_ok=True)
        raise


def _validate_existing(root: Path, secrets_path: Path) -> None:
    _validate_root_boundary(root)
    if secrets_path != root / "secrets.env":
        raise ValueError("Production secrets path is invalid")
    if not _private_directory(root):
        raise ValueError("Production runtime directory is unsafe")
    for name in ("data", "backups", "operations"):
        if not _private_directory(root / name):
            raise ValueError("Production runtime directory is unsafe")
    values = load_secrets_environment(secrets_path)
    secrets_metadata = secrets_path.lstat()
    if secrets_metadata.st_uid != os.getuid():
        raise ValueError("Production secrets file owner is unsafe")
    validate_key_environment(values)
    validate_schwab_environment(values)
    if values.get("NORTHSTAR_ENV") != "production" or values.get("NORTHSTAR_AUTH_MODE") != "cloudflare":
        raise ValueError("Production runtime mode is invalid")
    if values.get("NORTHSTAR_SECURE_COOKIES") != "1":
        raise ValueError("Production cookies are unsafe")
    if values.get("NORTHSTAR_BIND_ADDRESS") != "127.0.0.1":
        raise ValueError("Production bind address is unsafe")
    replication = values.get("NORTHSTAR_REPLICATION_DIR", "").strip()
    if replication and Path(replication).absolute() != RECOVERY_DESTINATION:
        raise ValueError("Production replication path is invalid")
    validate_cloudflare_issuer(values.get("NORTHSTAR_CF_ISSUER"))
    validate_cloudflare_audience(values.get("NORTHSTAR_CF_AUDIENCE"))
    normalize_allowed_email(values.get("NORTHSTAR_ALLOWED_EMAIL"))
    expected_paths = {
        "NORTHSTAR_DATABASE_PATH": root / "data/northstar.db",
        "NORTHSTAR_BACKUP_DIR": root / "backups",
        "NORTHSTAR_RUNTIME_ROOT": root,
        "NORTHSTAR_OPERATIONS_DIR": root / "operations",
        "NORTHSTAR_SECRETS_PATH": secrets_path,
    }
    if any(Path(values.get(name, "")).absolute() != expected for name, expected in expected_paths.items()):
        raise ValueError("Production runtime path is invalid")


def _key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _validate_root_boundary(root: Path) -> None:
    if root.resolve().is_relative_to(PROJECT_ROOT):
        raise ValueError("Production runtime must remain outside the repository")
    if os.path.lexists(root) and stat.S_ISLNK(root.lstat().st_mode):
        raise ValueError("Production runtime directory is unsafe")


def _private_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and metadata.st_mode & 0o077 == 0
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
