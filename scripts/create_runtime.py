#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import os
import secrets
from pathlib import Path


def key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create Northstar runtime directories and local-only secrets."
    )
    parser.add_argument("--root", default="~/.northstar", help="Runtime directory outside the Git repository")
    parser.add_argument(
        "--production-synthetic",
        action="store_true",
        help="Create fake production-mode settings for operational smoke tests only.",
    )
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    data_dir = root / "data"
    backup_dir = root / "backups"
    operations_dir = root / "operations"
    secrets_path = root / "secrets.env"

    if secrets_path.exists():
        raise SystemExit(f"Refusing to overwrite existing secrets: {secrets_path}")

    for directory in (root, data_dir, backup_dir, operations_dir):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)

    values = {
        "NORTHSTAR_ENV": "production" if args.production_synthetic else "development",
        "NORTHSTAR_AUTH_MODE": "cloudflare" if args.production_synthetic else "local",
        "NORTHSTAR_DATABASE_PATH": str(data_dir / "northstar.db"),
        "NORTHSTAR_BACKUP_DIR": str(backup_dir),
        "NORTHSTAR_RUNTIME_ROOT": str(root),
        "NORTHSTAR_OPERATIONS_DIR": str(operations_dir),
        "NORTHSTAR_SECRETS_PATH": str(secrets_path),
        "NORTHSTAR_DATABASE_KEY": key(),
        "NORTHSTAR_DATA_KEY": key(),
        "NORTHSTAR_BACKUP_KEY": key(),
        "NORTHSTAR_BLIND_INDEX_KEY": key(),
        "NORTHSTAR_SESSION_KEY": secrets.token_urlsafe(48),
        "NORTHSTAR_KEY_VERSION": "1",
        "NORTHSTAR_BACKUP_DAILY_RETENTION": "14",
        "NORTHSTAR_BACKUP_MONTHLY_RETENTION": "12",
        "NORTHSTAR_SECURE_COOKIES": "1" if args.production_synthetic else "0",
        "NORTHSTAR_BIND_ADDRESS": "127.0.0.1",
        "NORTHSTAR_CONTAINER_NAME": "northstar-local",
        "NORTHSTAR_EXPECTED_CONTAINER_USER": "10001:10001",
        "NORTHSTAR_MINIMUM_FREE_BYTES": str(512 * 1024 * 1024),
    }
    if args.production_synthetic:
        values.update(
            {
                "NORTHSTAR_CF_ISSUER": "https://synthetic-team.cloudflareaccess.com",
                "NORTHSTAR_CF_AUDIENCE": "synthetic-audience",
                "NORTHSTAR_ALLOWED_EMAIL": "synthetic@northstar.invalid",
            }
        )
    body = "\n".join(f"{name}={value}" for name, value in values.items()) + "\n"
    descriptor = os.open(secrets_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(body)
    secrets_path.chmod(0o600)

    print(f"Runtime directory created: {root}")
    print("No Schwab or Cloudflare credentials were created or requested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
