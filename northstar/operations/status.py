from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from northstar.operations.preflight import ProductionPreflight
from northstar.operations.runtime import OperationalRuntime
from northstar.services.backup import BACKUP_NAME


@dataclass(frozen=True)
class OperationalStatus:
    application_health: str
    database_health: str
    schema_health: str
    last_successful_backup: str | None
    last_successful_restore_drill: str | None
    last_successful_replication: str | None
    backup_count: int
    disk_space_warning: bool
    production_preflight: str
    last_failure_code: str | None
    pending_recovery_schema_version: int | None

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


def collect_operational_status(
    runtime: OperationalRuntime,
    *,
    preflight: ProductionPreflight | None = None,
) -> OperationalStatus:
    active_preflight = preflight or ProductionPreflight(runtime.settings, runtime.operational)
    report = active_preflight.run()
    check_by_code = {check.code: check.passed for check in report.checks}
    database_health = runtime.database.health()
    try:
        runtime.database.verify_critical_invariants()
    except Exception:
        schema_health = False
    else:
        schema_health = True
    state = runtime.state.read()
    backup_count = sum(
        1
        for path in runtime.settings.backup_dir.iterdir()
        if BACKUP_NAME.fullmatch(path.name) is not None and path.is_file() and not path.is_symlink()
    )
    return OperationalStatus(
        "healthy" if check_by_code.get("container_identity", False) else "unhealthy",
        "healthy" if database_health else "unhealthy",
        "compatible" if schema_health else "incompatible",
        state.last_successful_backup,
        state.last_successful_restore_drill,
        state.last_successful_replication,
        backup_count,
        not check_by_code.get("disk_space", False),
        "pass" if report.passed else "fail",
        state.last_failure_code,
        state.pending_recovery_schema_version,
    )
