from __future__ import annotations

import json
import os
import plistlib
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from northstar.config import Settings
from northstar.data import Database
from northstar.data.database import LATEST_SCHEMA_VERSION, MIGRATION_2, MIGRATION_3, SCHEMA_V1, utc_now
from northstar.operations.config import OperationalConfig
from northstar.operations.deployment import DeploymentError, ProductionUpdater
from northstar.operations.jobs import ScheduledBackupJob
from northstar.operations.preflight import PreflightCheck, PreflightReport
from northstar.operations.runtime import OperationalRuntime
from northstar.operations.state import OperationalStateStore
from northstar.operations.status import collect_operational_status
from northstar.services import BackupService
from scripts import run_schwab_live_snapshot, update_production
from scripts.install_launchd import _validated_executable_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_launchd_installer_preserves_virtual_environment_entry_point(tmp_path: Path) -> None:
    venv_python = tmp_path / "venv-python"
    venv_python.symlink_to(sys.executable)

    validated = _validated_executable_path(venv_python)

    assert validated == venv_python.absolute()
    assert validated != Path(os.path.realpath(venv_python))


class PassingPreflight:
    def run(self) -> PreflightReport:
        return PreflightReport(
            tuple(
                PreflightCheck(code, True)
                for code in (
                    "filevault",
                    "production_config",
                    "key_material",
                    "runtime_paths",
                    "runtime_permissions",
                    "encrypted_storage",
                    "disk_space",
                    "docker_daemon",
                    "compose_version",
                    "compose_config",
                    "container_identity",
                    "loopback_publication",
                )
            )
        )


class RecordingBackupJob:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def run(self, **_: object) -> object:
        self.events.append("backup")
        return object()


def operational_runtime(settings: Settings, tmp_path: Path) -> OperationalRuntime:
    runtime_root = tmp_path / "runtime"
    operations = runtime_root / "operations"
    runtime_root.mkdir(mode=0o700)
    operations.mkdir(mode=0o700)
    operations.chmod(0o700)
    settings.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    database.initialize()
    database.upsert_secure_record("demo", "status", {"private-value": "invented-98765"})
    secrets = runtime_root / "secrets.env"
    secrets.write_text("synthetic only\n", encoding="utf-8")
    secrets.chmod(0o600)
    operational = OperationalConfig(
        PROJECT_ROOT,
        runtime_root,
        operations,
        None,
        secrets,
    )
    return OperationalRuntime(
        settings,
        operational,
        database,
        BackupService(database, settings.backup_dir, settings.backup_vault()),
        OperationalStateStore(operations),
        {},
    )


def test_live_snapshot_recovery_gate_uses_canonical_backup_time_not_mtime(
    settings: Settings,
    tmp_path: Path,
) -> None:
    runtime = operational_runtime(settings, tmp_path)
    created_at = datetime(2026, 8, 16, 10, tzinfo=UTC)
    runtime.backup_service.clock = lambda: created_at
    backup = runtime.backup_service.create(prune=False)
    fresh_filesystem_time = datetime(2026, 8, 16, 12, tzinfo=UTC).timestamp()
    os.utime(backup, (fresh_filesystem_time, fresh_filesystem_time))

    assert runtime.backup_service.creation_time(backup) == created_at
    assert not run_schwab_live_snapshot._recovery_checkpoint(
        runtime,
        created_at + timedelta(minutes=1),
    )


def test_docker_actions_supply_validated_host_paths_to_every_compose_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("NORTHSTAR_SECRET_FILE", "NORTHSTAR_DATA_DIR", "NORTHSTAR_BACKUP_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(update_production, "_binary", lambda name: name)
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(command: list[str], **options: object) -> SimpleNamespace:
        environment = options.get("env")
        assert isinstance(environment, dict)
        calls.append((command, environment))
        if command[1:] == ["rev-parse", "HEAD"]:
            output = "a" * 40 + "\n"
        else:
            output = "healthy\n" if command[1:3] == ["inspect", "--format"] else ""
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(update_production.subprocess, "run", run)
    secrets_path = tmp_path / "runtime" / "secrets.env"
    data_dir = tmp_path / "runtime" / "data"
    backup_dir = tmp_path / "runtime" / "backups"
    actions = update_production.DockerActions(
        "northstar-local",
        secrets_path=secrets_path,
        data_dir=data_dir,
        backup_dir=backup_dir,
    )

    actions.build_without_pull()
    actions.restart()
    actions.stop_application()
    actions.rollback_image()

    compose_calls = [call for call in calls if call[0][1:2] == ["compose"]]
    assert len(compose_calls) == 4
    for _, environment in compose_calls:
        assert environment["NORTHSTAR_SECRET_FILE"] == str(secrets_path)
        assert environment["NORTHSTAR_DATA_DIR"] == str(data_dir)
        assert environment["NORTHSTAR_BACKUP_DIR"] == str(backup_dir)
        assert environment["NORTHSTAR_BUILD_COMMIT"] == "a" * 40


def schema_three_database(settings: Settings) -> Database:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    settings.database_path.parent.mkdir(parents=True)
    with closing(database.connect()) as conn:
        conn.executescript(SCHEMA_V1)
        conn.executescript(MIGRATION_2)
        conn.executescript(MIGRATION_3)
        conn.execute(
            "INSERT INTO app_meta(key, value, updated_at) VALUES('schema_version', '3', ?)",
            (utc_now(),),
        )
        conn.commit()
    return database


def test_schema_three_to_latest_production_update_keeps_validated_recovery_points(
    settings: Settings, tmp_path: Path
) -> None:
    database = schema_three_database(settings)
    service = BackupService(database, settings.backup_dir, settings.backup_vault())
    operations_dir = tmp_path / "operations"
    operations_dir.mkdir(mode=0o700)
    state = OperationalStateStore(operations_dir)
    events: list[str] = []

    result = ProductionUpdater(
        repository_clean=lambda: True,
        preflight=lambda: True,
        backup_job=ScheduledBackupJob(service, state, operations_dir),
        preserve_current_image=lambda: events.append("preserve"),
        build_without_pull=lambda: events.append("build"),
        restart=database.initialize,
        wait_healthy=database.health,
        rollback_image=lambda: events.append("rollback"),
        stop_application=lambda: events.append("stop"),
        read_schema_version=database.schema_version,
        record_failure=state.failure,
        record_recovery_schema=state.recovery_schema,
    ).run()

    backups = sorted(settings.backup_dir.glob("northstar_*.nsbak"))
    assert result.source_schema_version == 3
    assert result.target_schema_version == LATEST_SCHEMA_VERSION
    assert database.schema_version() == LATEST_SCHEMA_VERSION
    assert len(backups) == 2
    assert service.restore_drill(backups[0], expected_schema_version=3).schema_version == 3
    assert (
        service.restore_drill(backups[1], expected_schema_version=LATEST_SCHEMA_VERSION).schema_version
        == LATEST_SCHEMA_VERSION
    )
    assert events == ["preserve", "build"]
    assert state.read().pending_recovery_schema_version is None


def test_operational_status_is_privacy_safe(settings: Settings, tmp_path: Path) -> None:
    runtime = operational_runtime(settings, tmp_path)
    runtime.backup_service.create()
    runtime.state.success("backup")
    runtime.state.success("restore_drill")
    runtime.state.success("replication")
    runtime.state.recovery_schema(3)

    status = collect_operational_status(runtime, preflight=PassingPreflight())  # type: ignore[arg-type]
    rendered = json.dumps(status.safe_dict())

    assert status.application_health == "healthy"
    assert status.database_health == "healthy"
    assert status.schema_health == "compatible"
    assert status.backup_count == 1
    assert status.pending_recovery_schema_version == 3
    assert "northstar_" not in rendered
    assert "northstar.db" not in rendered
    assert "invented-98765" not in rendered
    assert str(runtime.operational.runtime_root) not in rendered
    assert "private-value" not in rendered


def test_update_always_backs_up_before_build_restart_and_health_validation() -> None:
    events: list[str] = []

    def schema_version() -> int:
        events.append("schema")
        return 4

    result = ProductionUpdater(
        repository_clean=lambda: events.append("clean") is None,
        preflight=lambda: events.append("preflight") is None,
        backup_job=RecordingBackupJob(events),  # type: ignore[arg-type]
        preserve_current_image=lambda: events.append("preserve"),
        build_without_pull=lambda: events.append("build"),
        restart=lambda: events.append("restart"),
        wait_healthy=lambda: events.append("health") is None,
        rollback_image=lambda: events.append("rollback"),
        stop_application=lambda: events.append("stop"),
        read_schema_version=schema_version,
        record_failure=lambda code: events.append(code),
        record_recovery_schema=lambda version: events.append(f"recovery:{version}"),
    ).run()

    assert result.status == "updated"
    assert events == [
        "clean",
        "preflight",
        "schema",
        "backup",
        "preserve",
        "recovery:4",
        "build",
        "restart",
        "health",
        "preflight",
        "schema",
        "backup",
        "recovery:None",
    ]


def test_failed_preflight_stops_before_backup_build_or_restart() -> None:
    events: list[str] = []
    updater = ProductionUpdater(
        repository_clean=lambda: events.append("clean") is None,
        preflight=lambda: events.append("preflight") is not None,
        backup_job=RecordingBackupJob(events),  # type: ignore[arg-type]
        preserve_current_image=lambda: events.append("preserve"),
        build_without_pull=lambda: events.append("build"),
        restart=lambda: events.append("restart"),
        wait_healthy=lambda: events.append("health") is None,
        rollback_image=lambda: events.append("rollback"),
        stop_application=lambda: events.append("stop"),
        read_schema_version=lambda: events.append("schema") or 4,
        record_failure=lambda code: events.append(code),
        record_recovery_schema=lambda version: events.append(f"recovery:{version}"),
    )

    with pytest.raises(DeploymentError, match="Production preflight failed"):
        updater.run()

    assert events == ["clean", "preflight"]


def test_failed_restart_automatically_restores_previous_image_after_backup() -> None:
    events: list[str] = []

    def fail_restart() -> None:
        events.append("restart")
        raise RuntimeError("synthetic restart failure")

    updater = ProductionUpdater(
        repository_clean=lambda: True,
        preflight=lambda: True,
        backup_job=RecordingBackupJob(events),  # type: ignore[arg-type]
        preserve_current_image=lambda: events.append("preserve"),
        build_without_pull=lambda: events.append("build"),
        restart=fail_restart,
        wait_healthy=lambda: True,
        rollback_image=lambda: events.append("rollback"),
        stop_application=lambda: events.append("stop"),
        read_schema_version=lambda: 3,
        record_failure=lambda code: events.append(code),
        record_recovery_schema=lambda _: None,
    )

    with pytest.raises(DeploymentError, match="production_update_rolled_back"):
        updater.run()

    assert events == [
        "backup",
        "preserve",
        "build",
        "restart",
        "rollback",
        "production_update_rolled_back",
    ]


def test_failed_update_after_migration_never_starts_previous_image() -> None:
    events: list[str] = []
    schema_versions = iter((3, 4))
    updater = ProductionUpdater(
        repository_clean=lambda: True,
        preflight=lambda: True,
        backup_job=RecordingBackupJob(events),  # type: ignore[arg-type]
        preserve_current_image=lambda: events.append("preserve"),
        build_without_pull=lambda: events.append("build"),
        restart=lambda: events.append("restart"),
        wait_healthy=lambda: False,
        rollback_image=lambda: events.append("rollback"),
        stop_application=lambda: events.append("stop"),
        read_schema_version=lambda: next(schema_versions),
        record_failure=lambda code: events.append(code),
        record_recovery_schema=lambda _: None,
    )

    with pytest.raises(DeploymentError, match="production_update_migration_requires_recovery") as raised:
        updater.run()

    assert "rollback" not in events
    assert "stop" in events
    assert events[-1] == "production_update_migration_requires_recovery"
    assert str(raised.value) == "production_update_migration_requires_recovery"


def test_unknown_schema_failure_stops_application_without_rollback() -> None:
    events: list[str] = []
    schema_reads = 0

    def read_schema() -> int:
        nonlocal schema_reads
        schema_reads += 1
        if schema_reads == 1:
            return 3
        raise RuntimeError("synthetic schema read failure")

    updater = ProductionUpdater(
        repository_clean=lambda: True,
        preflight=lambda: True,
        backup_job=RecordingBackupJob(events),  # type: ignore[arg-type]
        preserve_current_image=lambda: events.append("preserve"),
        build_without_pull=lambda: events.append("build"),
        restart=lambda: events.append("restart"),
        wait_healthy=lambda: False,
        rollback_image=lambda: events.append("rollback"),
        stop_application=lambda: events.append("stop"),
        read_schema_version=read_schema,
        record_failure=lambda code: events.append(code),
        record_recovery_schema=lambda _: None,
    )

    with pytest.raises(DeploymentError, match="production_update_schema_unknown"):
        updater.run()

    assert "stop" in events
    assert "rollback" not in events
    assert events[-1] == "production_update_schema_unknown"


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (
            DeploymentError("production_update_migration_requires_recovery"),
            "production_update_migration_requires_recovery",
        ),
        (DeploymentError("private path and exception text"), "production_update_failed"),
        (RuntimeError("private path and exception text"), "production_update_failed"),
    ],
)
def test_update_command_reports_only_allow_listed_failure_codes(
    failure: Exception,
    expected_code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingUpdater:
        def __init__(self, **_: object) -> None:
            pass

        def run(self) -> None:
            raise failure from RuntimeError("private runtime context")

    runtime = SimpleNamespace(
        operational=SimpleNamespace(
            replication_dir=None,
            operations_dir=tmp_path,
            container_name="synthetic-container",
            secrets_path=tmp_path / "secrets.env",
        ),
        settings=SimpleNamespace(
            database_path=tmp_path / "data" / "northstar.db",
            backup_dir=tmp_path,
            backup_vault=lambda: object(),
        ),
        backup_service=object(),
        state=SimpleNamespace(failure=lambda _: None, recovery_schema=lambda _: None),
        database=SimpleNamespace(schema_version=lambda: 4),
    )
    monkeypatch.setattr(update_production, "load_operational_runtime", lambda *_, **__: runtime)
    monkeypatch.setattr(update_production, "ProductionUpdater", FailingUpdater)
    monkeypatch.setattr(
        update_production,
        "DockerActions",
        lambda *_, **__: SimpleNamespace(
            repository_clean=lambda: True,
            preserve_current_image=lambda: None,
            build_without_pull=lambda: None,
            restart=lambda: None,
            wait_healthy=lambda: True,
            rollback_image=lambda: None,
            stop_application=lambda: None,
        ),
    )
    monkeypatch.setattr(
        update_production,
        "ProductionPreflight",
        lambda *_: SimpleNamespace(run=lambda: SimpleNamespace(passed=True)),
    )
    monkeypatch.setattr(sys, "argv", ["update_production.py", "--secrets-file", str(tmp_path)])

    assert update_production.main() == 1
    rendered = capsys.readouterr().out
    assert json.loads(rendered) == {"status": "fail", "code": expected_code}
    assert "private" not in rendered


def test_post_migration_failure_preserves_database_sidecars_and_source_backup(
    settings: Settings, tmp_path: Path
) -> None:
    database = schema_three_database(settings)
    service = BackupService(database, settings.backup_dir, settings.backup_vault())
    operations_dir = tmp_path / "operations"
    operations_dir.mkdir(mode=0o700)
    state = OperationalStateStore(operations_dir)
    open_connections = []
    preserved: dict[Path, bytes] = {}
    rollback_attempted = False
    application_stopped = False

    def migrate_and_hold_sidecars() -> None:
        database.initialize()
        connection = database.connect()
        connection.execute("UPDATE app_meta SET updated_at=updated_at WHERE key='schema_version'")
        connection.commit()
        open_connections.append(connection)

    def capture_failed_health() -> bool:
        return False

    def stop_application() -> None:
        nonlocal application_stopped
        for connection in open_connections:
            connection.close()
        open_connections.clear()
        paths = (
            database.path,
            Path(f"{database.path}-wal"),
            Path(f"{database.path}-shm"),
        )
        for path in paths[1:]:
            if not path.exists():
                path.write_bytes(b"synthetic-preserved-sidecar")
        preserved.update((path, path.read_bytes()) for path in paths)
        application_stopped = True

    def rollback() -> None:
        nonlocal rollback_attempted
        rollback_attempted = True

    updater = ProductionUpdater(
        repository_clean=lambda: True,
        preflight=lambda: True,
        backup_job=ScheduledBackupJob(service, state, operations_dir),
        preserve_current_image=lambda: None,
        build_without_pull=lambda: None,
        restart=migrate_and_hold_sidecars,
        wait_healthy=capture_failed_health,
        rollback_image=rollback,
        stop_application=stop_application,
        read_schema_version=database.schema_version,
        record_failure=state.failure,
        record_recovery_schema=state.recovery_schema,
    )

    try:
        with pytest.raises(DeploymentError, match="production_update_migration_requires_recovery"):
            updater.run()

        backups = list(settings.backup_dir.glob("northstar_*.nsbak"))
        assert not rollback_attempted
        assert application_stopped
        assert len(backups) == 1
        assert service.restore_drill(backups[0], expected_schema_version=3).schema_version == 3
        assert {path: path.read_bytes() for path in preserved} == preserved
        assert state.read().last_failure_code == "production_update_migration_requires_recovery"
        assert state.read().pending_recovery_schema_version == 3
    finally:
        for connection in open_connections:
            connection.close()


def test_failed_backup_prevents_any_update_action() -> None:
    events: list[str] = []

    class FailedBackup:
        def run(self, **_: object) -> None:
            events.append("backup")
            raise RuntimeError("synthetic backup failure")

    updater = ProductionUpdater(
        repository_clean=lambda: True,
        preflight=lambda: True,
        backup_job=FailedBackup(),  # type: ignore[arg-type]
        preserve_current_image=lambda: events.append("preserve"),
        build_without_pull=lambda: events.append("build"),
        restart=lambda: events.append("restart"),
        wait_healthy=lambda: True,
        rollback_image=lambda: events.append("rollback"),
        stop_application=lambda: events.append("stop"),
        read_schema_version=lambda: 4,
        record_failure=lambda code: events.append(code),
        record_recovery_schema=lambda _: None,
    )

    with pytest.raises(RuntimeError, match="backup failure"):
        updater.run()

    assert events == ["backup"]


def test_launchd_schedules_contain_no_credentials_or_key_material() -> None:
    backup_path = PROJECT_ROOT / "deployment/mac-mini/com.northstar.local.backup.plist"
    recovery_path = PROJECT_ROOT / "deployment/mac-mini/com.northstar.local.recovery.plist"
    forbidden = (
        "NORTHSTAR_DATABASE_KEY",
        "NORTHSTAR_DATA_KEY",
        "NORTHSTAR_BACKUP_KEY",
        "NORTHSTAR_BLIND_INDEX_KEY",
        "NORTHSTAR_SESSION_KEY",
        "client_secret",
        "access_token",
    )

    backup = plistlib.loads(backup_path.read_bytes())
    recovery = plistlib.loads(recovery_path.read_bytes())
    combined = backup_path.read_text(encoding="utf-8") + recovery_path.read_text(encoding="utf-8")

    assert backup["StartCalendarInterval"] == {"Hour": 3, "Minute": 15}
    assert recovery["StartCalendarInterval"] == {"Day": 1, "Hour": 4, "Minute": 15}
    assert backup["Umask"] == 63 and recovery["Umask"] == 63
    assert not any(value in combined for value in forbidden)


def test_compose_defines_restart_graceful_shutdown_resource_and_log_bounds() -> None:
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    for expected in (
        '"127.0.0.1:${NORTHSTAR_PORT:-8010}:8000"',
        "restart: unless-stopped",
        "stop_grace_period: 30s",
        'cpus: "1.00"',
        "mem_limit: 512m",
        "pids_limit: 128",
        'max-size: "10m"',
        'max-file: "3"',
        "read_only: true",
        "no-new-privileges:true",
        "format: raw",
    ):
        assert expected in compose

    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.13.5-slim-bookworm@sha256:" in dockerfile
    assert "--constraint constraints-production.txt" in dockerfile
    assert "USER 10001:10001" in dockerfile

    constraints = (PROJECT_ROOT / "constraints-production.txt").read_text(encoding="utf-8")
    package_lines = [line for line in constraints.splitlines() if line and not line.startswith("#")]
    assert package_lines
    assert all("==" in line for line in package_lines)
