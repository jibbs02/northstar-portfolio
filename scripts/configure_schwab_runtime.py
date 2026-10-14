#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path

from northstar.config import (
    PRODUCTION_HOSTNAME,
    validate_production_hostname,
    validate_schwab_client_value,
)
from northstar.operations.config import (
    SCHWAB_ENV_NAMES,
    SCHWAB_ORDER_THROTTLE_VERIFICATION,
    load_secrets_environment,
    validate_key_environment,
    validate_schwab_environment,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_REDIRECT_URI = (
    f"https://{PRODUCTION_HOSTNAME}/integrations/schwab/callback"
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Configure Schwab OAuth in an external Northstar production secrets file."
    )
    parser.add_argument("--secrets-file", required=True, type=Path)
    parser.add_argument(
        "--disable",
        action="store_true",
        help="Disable Schwab and remove its provider credentials from the secrets file.",
    )
    args = parser.parse_args()
    try:
        path = args.secrets_file.expanduser().absolute()
        if args.disable:
            _disable(path)
            result = "disabled"
        else:
            verification = input(
                "Confirm Schwab PUT/POST/DELETE order throttle is exactly 0 "
                "(type ZERO): "
            ).strip()
            if verification != "ZERO":
                raise ValueError("Schwab zero order throttle was not verified")
            client_id = validate_schwab_client_value(
                getpass.getpass("Schwab app key/client ID (hidden): "),
                "client ID",
                512,
            )
            provider_credential = validate_schwab_client_value(
                getpass.getpass("Schwab app secret (hidden): "),
                "client secret",
                2_048,
            )
            _configure(
                path,
                client_id=client_id,
                client_secret=provider_credential,
                order_throttle_verified=True,
            )
            result = "configured"
    except Exception:
        print("schwab_runtime: fail")
        return 1
    print(f"schwab_runtime: {result}")
    print("schwab_runtime: production_restart_required")
    return 0


def _configure(
    path: Path,
    *,
    client_id: str,
    client_secret: str,
    order_throttle_verified: bool,
) -> None:
    if order_throttle_verified is not True:
        raise ValueError("Schwab zero order throttle was not verified")
    values = _validated_values(path)
    updated = dict(values)
    updated.update(
        {
            "NORTHSTAR_SCHWAB_ENABLED": "1",
            SCHWAB_ORDER_THROTTLE_VERIFICATION: "1",
            "SCHWAB_CLIENT_ID": validate_schwab_client_value(client_id, "client ID", 512),
            "SCHWAB_CLIENT_SECRET": validate_schwab_client_value(
                client_secret, "client secret", 2_048
            ),
            "SCHWAB_REDIRECT_URI": "https://" + validate_production_hostname(
                values.get("NORTHSTAR_PRODUCTION_HOSTNAME", PRODUCTION_HOSTNAME)
            ) + "/integrations/schwab/callback",
        }
    )
    validate_schwab_environment(updated)
    _replace(path, updated)


def _disable(path: Path) -> None:
    values = _validated_values(path)
    updated = dict(values)
    updated["NORTHSTAR_SCHWAB_ENABLED"] = "0"
    updated[SCHWAB_ORDER_THROTTLE_VERIFICATION] = "0"
    for name in SCHWAB_ENV_NAMES:
        updated.pop(name, None)
    validate_schwab_environment(updated)
    _replace(path, updated)


def _validated_values(path: Path) -> dict[str, str]:
    _validate_target(path)
    values = load_secrets_environment(path)
    validate_key_environment(values)
    if values.get("NORTHSTAR_ENV") != "production":
        raise ValueError("Schwab runtime configuration requires production mode")
    return values


def _validate_target(path: Path) -> None:
    if path.resolve().is_relative_to(PROJECT_ROOT):
        raise ValueError("Production secrets must remain outside the repository")
    metadata = path.lstat()
    parent_metadata = path.parent.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
        or not metadata.st_mode & stat.S_IRUSR
        or not metadata.st_mode & stat.S_IWUSR
    ):
        raise ValueError("Production secrets file is unsafe")
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.getuid()
        or parent_metadata.st_mode & 0o077
    ):
        raise ValueError("Production secrets directory is unsafe")


def _replace(path: Path, values: Mapping[str, str]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".schwab-secrets-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = -1
        with handle:
            for name, value in values.items():
                handle.write(f"{name}={value}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        _fsync_directory(path.parent)
        validate_schwab_environment(load_secrets_environment(path))
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
