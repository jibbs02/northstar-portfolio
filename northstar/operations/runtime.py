from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from northstar.config import Settings
from northstar.data import Database
from northstar.operations.config import (
    OperationalConfig,
    apply_environment,
    load_secrets_environment,
    validate_key_environment,
    validate_schwab_environment,
)
from northstar.operations.state import OperationalStateStore
from northstar.services import BackupRetentionPolicy, BackupService


@dataclass(frozen=True)
class OperationalRuntime:
    settings: Settings
    operational: OperationalConfig
    database: Database
    backup_service: BackupService
    state: OperationalStateStore
    environment: dict[str, str]


def load_operational_runtime(
    secrets_path: Path,
    *,
    repository_root: Path,
) -> OperationalRuntime:
    values = load_secrets_environment(secrets_path)
    validate_key_environment(values)
    validate_schwab_environment(values)
    apply_environment(values)
    settings = Settings.from_env()
    settings.validate_runtime_permissions()
    operational = OperationalConfig.from_environment(
        values,
        repository_root=repository_root,
        database_path=settings.database_path,
        backup_dir=settings.backup_dir,
        secrets_path=secrets_path,
    )
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    backup_service = BackupService(
        database,
        settings.backup_dir,
        settings.backup_vault(),
        BackupRetentionPolicy(
            settings.backup_daily_retention,
            settings.backup_monthly_retention,
        ),
    )
    return OperationalRuntime(
        settings,
        operational,
        database,
        backup_service,
        OperationalStateStore(operational.operations_dir),
        values,
    )
