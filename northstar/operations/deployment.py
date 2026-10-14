from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from northstar.operations.jobs import ScheduledBackupJob


class DeploymentError(RuntimeError):
    """Raised when a constrained production update cannot be completed safely."""


PRODUCTION_UPDATE_ERROR_CODES = frozenset(
    {
        "production_update_migration_requires_recovery",
        "production_update_rollback_failed",
        "production_update_rolled_back",
        "production_update_schema_unknown",
        "production_update_stop_failed",
    }
)


@dataclass(frozen=True)
class DeploymentResult:
    status: str
    rollback_attempted: bool
    source_schema_version: int
    target_schema_version: int


class ProductionUpdater:
    """Perform a fail-closed, migration-aware production application update."""

    def __init__(
        self,
        *,
        repository_clean: Callable[[], bool],
        preflight: Callable[[], bool],
        backup_job: ScheduledBackupJob,
        preserve_current_image: Callable[[], None],
        build_without_pull: Callable[[], None],
        restart: Callable[[], None],
        wait_healthy: Callable[[], bool],
        rollback_image: Callable[[], None],
        stop_application: Callable[[], None],
        read_schema_version: Callable[[], int],
        record_failure: Callable[[str], object],
        record_recovery_schema: Callable[[int | None], object],
    ) -> None:
        self.repository_clean = repository_clean
        self.preflight = preflight
        self.backup_job = backup_job
        self.preserve_current_image = preserve_current_image
        self.build_without_pull = build_without_pull
        self.restart = restart
        self.wait_healthy = wait_healthy
        self.rollback_image = rollback_image
        self.stop_application = stop_application
        self.read_schema_version = read_schema_version
        self.record_failure = record_failure
        self.record_recovery_schema = record_recovery_schema

    def run(self) -> DeploymentResult:
        if not self.repository_clean():
            raise DeploymentError("Production repository is not clean")
        if not self.preflight():
            raise DeploymentError("Production preflight failed")
        source_schema_version = self.read_schema_version()
        self.backup_job.run(expected_schema_version=source_schema_version, prune=False)
        self.preserve_current_image()
        self.record_recovery_schema(source_schema_version)
        try:
            self.build_without_pull()
            self.restart()
            if not self.wait_healthy() or not self.preflight():
                raise DeploymentError("Updated application failed validation")
            target_schema_version = self.read_schema_version()
            if target_schema_version < source_schema_version:
                raise DeploymentError("Updated application schema is incompatible")
            self.backup_job.run(expected_schema_version=target_schema_version, prune=False)
        except Exception as exc:
            try:
                current_schema_version = self.read_schema_version()
            except Exception as schema_exc:
                self._stop_for_recovery()
                self.record_failure("production_update_schema_unknown")
                raise DeploymentError("production_update_schema_unknown") from schema_exc
            if current_schema_version != source_schema_version:
                self._stop_for_recovery()
                self.record_failure("production_update_migration_requires_recovery")
                raise DeploymentError("production_update_migration_requires_recovery") from exc
            try:
                self.rollback_image()
            except Exception as rollback_exc:
                self.record_failure("production_update_rollback_failed")
                raise DeploymentError("production_update_rollback_failed") from rollback_exc
            self.record_recovery_schema(None)
            self.record_failure("production_update_rolled_back")
            raise DeploymentError("production_update_rolled_back") from exc
        self.record_recovery_schema(None)
        return DeploymentResult("updated", False, source_schema_version, target_schema_version)

    def _stop_for_recovery(self) -> None:
        try:
            self.stop_application()
        except Exception as stop_exc:
            self.record_failure("production_update_stop_failed")
            raise DeploymentError("production_update_stop_failed") from stop_exc
