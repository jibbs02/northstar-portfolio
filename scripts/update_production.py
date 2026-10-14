#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from northstar.operations import ReplicationService, ScheduledBackupJob
from northstar.operations.deployment import (
    PRODUCTION_UPDATE_ERROR_CODES,
    DeploymentError,
    ProductionUpdater,
)
from northstar.operations.preflight import ProductionPreflight
from northstar.operations.runtime import load_operational_runtime

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_IMAGE = "northstar-local:production"
ROLLBACK_IMAGE = "northstar-local:rollback"


class DockerActions:
    def __init__(
        self,
        container_name: str,
        *,
        secrets_path: Path,
        data_dir: Path,
        backup_dir: Path,
    ) -> None:
        self.container_name = container_name
        self.git = _binary("git")
        self.docker = _binary("docker")
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "NORTHSTAR_SECRET_FILE": str(secrets_path),
                "NORTHSTAR_DATA_DIR": str(data_dir),
                "NORTHSTAR_BACKUP_DIR": str(backup_dir),
            }
        )
        self.environment["NORTHSTAR_BUILD_COMMIT"] = self._reviewed_commit()

    def _reviewed_commit(self) -> str:
        result = self._run([self.git, "rev-parse", "HEAD"])
        value = result.stdout.strip().lower()
        if result.returncode != 0 or len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
            raise RuntimeError("Reviewed commit identity is unavailable")
        return value

    def repository_clean(self) -> bool:
        result = self._run([self.git, "status", "--porcelain"])
        return result.returncode == 0 and not result.stdout.strip()

    def preserve_current_image(self) -> None:
        result = self._run([self.docker, "inspect", "--format", "{{.Image}}", self.container_name])
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError("Current application image is unavailable")
        tagged = self._run([self.docker, "image", "tag", result.stdout.strip(), ROLLBACK_IMAGE])
        if tagged.returncode != 0:
            raise RuntimeError("Current application image could not be preserved")

    def build_without_pull(self) -> None:
        result = self._run([self.docker, "compose", "build", "--pull=false", "app"])
        if result.returncode != 0:
            raise RuntimeError("Production image build failed")

    def restart(self) -> None:
        result = self._run([self.docker, "compose", "up", "--detach", "--no-build", "app"])
        if result.returncode != 0:
            raise RuntimeError("Production container restart failed")

    def wait_healthy(self) -> bool:
        for _ in range(12):
            result = self._run(
                [
                    self.docker,
                    "inspect",
                    "--format",
                    "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                    self.container_name,
                ]
            )
            if result.returncode == 0 and result.stdout.strip() == "healthy":
                return True
            time.sleep(5)
        return False

    def rollback_image(self) -> None:
        if self._run([self.docker, "image", "tag", ROLLBACK_IMAGE, PRODUCTION_IMAGE]).returncode != 0:
            raise RuntimeError("Rollback image is unavailable")
        self.restart()
        if not self.wait_healthy():
            raise RuntimeError("Rollback image did not become healthy")

    def stop_application(self) -> None:
        if self._run([self.docker, "compose", "stop", "app"]).returncode != 0:
            raise RuntimeError("Production application could not be stopped")

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            command,
            cwd=PROJECT_ROOT,
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Update Northstar after an encrypted recovery point.")
    parser.add_argument("--secrets-file", required=True, type=Path)
    args = parser.parse_args()
    try:
        runtime = load_operational_runtime(args.secrets_file.expanduser(), repository_root=PROJECT_ROOT)
        replication = (
            ReplicationService(
                runtime.settings.backup_dir,
                runtime.operational.replication_dir,
                runtime.settings.backup_vault(),
                repository_root=PROJECT_ROOT,
                runtime_root=runtime.operational.runtime_root,
            )
            if runtime.operational.replication_dir is not None
            else None
        )
        backup_job = ScheduledBackupJob(
            runtime.backup_service,
            runtime.state,
            runtime.operational.operations_dir,
            replication=replication,
        )
        actions = DockerActions(
            runtime.operational.container_name,
            secrets_path=runtime.operational.secrets_path,
            data_dir=runtime.settings.database_path.parent,
            backup_dir=runtime.settings.backup_dir,
        )
        preflight = ProductionPreflight(runtime.settings, runtime.operational)
        ProductionUpdater(
            repository_clean=actions.repository_clean,
            preflight=lambda: preflight.run().passed,
            backup_job=backup_job,
            preserve_current_image=actions.preserve_current_image,
            build_without_pull=actions.build_without_pull,
            restart=actions.restart,
            wait_healthy=actions.wait_healthy,
            rollback_image=actions.rollback_image,
            stop_application=actions.stop_application,
            read_schema_version=runtime.database.schema_version,
            record_failure=runtime.state.failure,
            record_recovery_schema=runtime.state.recovery_schema,
        ).run()
    except DeploymentError as exc:
        code = str(exc)
        if code not in PRODUCTION_UPDATE_ERROR_CODES:
            code = "production_update_failed"
        print(json.dumps({"status": "fail", "code": code}))
        return 1
    except Exception:
        print(json.dumps({"status": "fail", "code": "production_update_failed"}))
        return 1
    print(json.dumps({"status": "ok", "backup": "validated", "application": "healthy"}))
    return 0


def _binary(name: str) -> str:
    value = shutil.which(name)
    if value is None:
        raise RuntimeError("Required deployment command is unavailable")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
