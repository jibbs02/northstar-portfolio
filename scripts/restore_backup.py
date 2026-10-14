#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from northstar.config import Settings
from northstar.data import Database
from northstar.services import BackupRetentionPolicy, BackupService

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Atomically restore an encrypted Northstar backup.")
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument(
        "--expected-schema-version",
        type=int,
        help="Expected schema for an explicit migration-recovery backup.",
    )
    parser.add_argument(
        "--replace-production",
        action="store_true",
        help="Required when the destination is the configured runtime database.",
    )
    args = parser.parse_args()

    settings = Settings.from_env()
    settings.validate_runtime_permissions()
    destination = args.destination.expanduser().resolve()
    configured = settings.database_path.expanduser().resolve()
    if destination.is_relative_to(PROJECT_ROOT):
        raise SystemExit("Refusing to restore runtime data inside the source repository")
    if destination == configured and not args.replace_production:
        raise SystemExit("Use --replace-production after stopping the app to replace the runtime database")

    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    service = BackupService(
        database,
        settings.backup_dir,
        settings.backup_vault(),
        BackupRetentionPolicy(
            settings.backup_daily_retention,
            settings.backup_monthly_retention,
        ),
    )
    options: dict[str, object] = {}
    if args.expected_schema_version is not None:
        options["expected_schema_version"] = args.expected_schema_version
    service.restore_to(args.backup.expanduser().resolve(), destination, **options)
    print(f"Encrypted backup restored atomically to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
