#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from northstar.config import Settings
from northstar.data import Database
from northstar.services import BackupRetentionPolicy, BackupService


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an encrypted backup in isolation.")
    parser.add_argument("--backup", type=Path, help="Specific encrypted backup to verify.")
    parser.add_argument(
        "--expected-schema-version",
        type=int,
        help="Expected schema for an explicit migration-recovery backup.",
    )
    args = parser.parse_args()
    if args.expected_schema_version is not None and args.backup is None:
        parser.error("--expected-schema-version requires --backup")
    settings = Settings.from_env()
    settings.validate_runtime_permissions()
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
    service.restore_drill(args.backup.expanduser().resolve() if args.backup else None, **options)
    print("Isolated restore drill passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
