from __future__ import annotations

import base64
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from northstar.config import Settings
from northstar.data import Database
from northstar.operations.config import OperationalConfig, validate_key_environment
from northstar.operations.preflight import (
    ProductionPreflight,
    SystemProbe,
    parse_compose_version,
    parse_filevault_enabled,
)


class FakeProbe(SystemProbe):
    def __init__(
        self,
        *,
        filevault: str = "FileVault is On.",
        docker: bool = True,
        compose_version: str = "2.30.0",
        compose_config: bool = True,
        free_bytes: int = 10 * 1024 * 1024 * 1024,
        inspection: dict[str, object] | None = None,
    ) -> None:
        self.filevault = filevault
        self.docker = docker
        self.compose_version = compose_version
        self.compose_config = compose_config
        self.available_bytes = free_bytes
        self.inspection = inspection or safe_inspection()

    def filevault_status(self) -> str:
        return self.filevault

    def docker_info(self) -> bool:
        return self.docker

    def docker_compose_version(self) -> str:
        return self.compose_version

    def docker_compose_config(
        self,
        repository_root: Path,
        *,
        secrets_path: Path,
        data_dir: Path,
        backup_dir: Path,
    ) -> bool:
        assert repository_root.is_absolute()
        assert secrets_path.name == "secrets.env"
        assert data_dir.name == "data"
        assert backup_dir.name == "backups"
        return self.compose_config

    def container_inspect(self, container_name: str) -> dict[str, object] | None:
        assert container_name == "northstar-local"
        return self.inspection

    def free_bytes(self, path: Path) -> int:
        assert path.is_dir()
        return self.available_bytes


def safe_inspection() -> dict[str, object]:
    return {
        "Config": {"User": "10001:10001"},
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "HostConfig": {
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PortBindings": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8010"}]},
        },
    }


def preflight_stack(settings: Settings, tmp_path: Path) -> tuple[Settings, OperationalConfig, Database]:
    runtime_root = tmp_path / "runtime"
    data_dir = runtime_root / "data"
    backup_dir = runtime_root / "backups"
    operations_dir = runtime_root / "operations"
    for directory in (runtime_root, data_dir, backup_dir, operations_dir):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    secrets = runtime_root / "secrets.env"
    secrets.write_text("synthetic-only=true\n", encoding="utf-8")
    secrets.chmod(0o600)
    production = replace(
        settings,
        environment="production",
        auth_mode="cloudflare",
        database_path=data_dir / "northstar.db",
        backup_dir=backup_dir,
        secrets_path=secrets,
        cf_issuer="https://synthetic-team.cloudflareaccess.com",
        cf_audience="synthetic-audience",
        allowed_email="synthetic@northstar.invalid",
        secure_cookies=True,
    )
    database = Database(production.database_path, production.data_vault(), production.database_key)
    database.initialize()
    operational = OperationalConfig(
        repository_root=Path(__file__).resolve().parents[1],
        runtime_root=runtime_root,
        operations_dir=operations_dir,
        replication_dir=None,
        secrets_path=secrets,
    )
    return production, operational, database


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("FileVault is On.\n", True),
        (" FileVault   is ON ", True),
        ("FileVault is Off.\n", False),
        ("FileVault is On, but encryption is still in progress.", False),
        ("", False),
    ],
)
def test_filevault_preflight_parser_is_fail_closed(output: str, expected: bool) -> None:
    assert parse_filevault_enabled(output) is expected


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("2.30.0\n", (2, 30, 0)),
        ("v2.31.1", (2, 31, 1)),
        ("2.30.0-desktop.1", (2, 30, 0)),
        ("2.30", None),
        ("Docker Compose version v2.30.0", None),
        ("", None),
    ],
)
def test_compose_version_parser_is_fail_closed(
    output: str, expected: tuple[int, int, int] | None
) -> None:
    assert parse_compose_version(output) == expected


def test_complete_synthetic_preflight_passes(settings: Settings, tmp_path: Path) -> None:
    production, operational, _ = preflight_stack(settings, tmp_path)

    report = ProductionPreflight(production, operational, probe=FakeProbe()).run()

    assert report.passed
    assert set(report.safe_dict()) == {"status", "checks"}


@pytest.mark.parametrize(
    ("probe", "failed_check"),
    [
        (FakeProbe(compose_version="2.29.9"), "compose_version"),
        (FakeProbe(compose_version="not-a-version"), "compose_version"),
        (FakeProbe(compose_config=False), "compose_config"),
    ],
)
def test_compose_prerequisites_fail_closed_before_deployment(
    settings: Settings,
    tmp_path: Path,
    probe: FakeProbe,
    failed_check: str,
) -> None:
    production, operational, _ = preflight_stack(settings, tmp_path)

    report = ProductionPreflight(production, operational, probe=probe).run()

    checks = {check.code: check.passed for check in report.checks}
    assert checks[failed_check] is False


def test_unsafe_permissions_and_symlinked_paths_fail_preflight(settings: Settings, tmp_path: Path) -> None:
    production, operational, _ = preflight_stack(settings, tmp_path)
    production.backup_dir.chmod(0o755)
    permission_report = ProductionPreflight(production, operational, probe=FakeProbe()).run()
    production.backup_dir.chmod(0o700)
    real_operations = operational.operations_dir
    moved = operational.runtime_root / "real-operations"
    real_operations.rename(moved)
    real_operations.symlink_to(moved, target_is_directory=True)
    symlink_report = ProductionPreflight(production, operational, probe=FakeProbe()).run()

    assert not {check.code: check.passed for check in permission_report.checks}["runtime_permissions"]
    assert not {check.code: check.passed for check in symlink_report.checks}["runtime_permissions"]


def test_runtime_and_replication_paths_inside_repository_are_rejected(
    settings: Settings, tmp_path: Path
) -> None:
    production, operational, _ = preflight_stack(settings, tmp_path)
    replication = operational.runtime_root / "replication"
    replication.mkdir(mode=0o700)
    unsafe = replace(
        operational,
        repository_root=operational.runtime_root.parent,
        replication_dir=replication,
    )

    report = ProductionPreflight(production, unsafe, probe=FakeProbe()).run()

    assert not {check.code: check.passed for check in report.checks}["runtime_paths"]


def test_root_or_malformed_expected_container_identity_fails_preflight(
    settings: Settings, tmp_path: Path
) -> None:
    production, operational, _ = preflight_stack(settings, tmp_path)

    for identity in ("0:0", "10001", "not-a-user"):
        report = ProductionPreflight(
            production,
            replace(operational, expected_container_user=identity),
            probe=FakeProbe(),
        ).run()
        checks = {check.code: check.passed for check in report.checks}
        assert not checks["production_config"]


def test_low_disk_plaintext_and_unsafe_publication_fail_closed(settings: Settings, tmp_path: Path) -> None:
    production, operational, _ = preflight_stack(settings, tmp_path)
    plaintext = operational.runtime_root / "forbidden.db"
    plaintext.write_bytes(b"SQLite format 3\x00synthetic")
    plaintext.chmod(0o600)
    inspection = safe_inspection()
    inspection["HostConfig"]["PortBindings"] = {  # type: ignore[index]
        "8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8010"}]  # noqa: S104
    }

    report = ProductionPreflight(
        production,
        operational,
        probe=FakeProbe(free_bytes=1, inspection=inspection),
    ).run()
    checks = {check.code: check.passed for check in report.checks}

    assert not checks["encrypted_storage"]
    assert not checks["disk_space"]
    assert not checks["loopback_publication"]


def test_safe_preflight_output_never_contains_paths_or_sensitive_values(
    settings: Settings, tmp_path: Path
) -> None:
    production, operational, _ = preflight_stack(settings, tmp_path)
    rendered = json.dumps(ProductionPreflight(production, operational, probe=FakeProbe()).run().safe_dict())

    assert str(operational.runtime_root) not in rendered
    assert "northstar.db" not in rendered
    assert base64.urlsafe_b64encode(production.database_key).decode() not in rendered
    assert "synthetic-audience" not in rendered


def test_missing_malformed_reused_and_short_key_material_is_rejected() -> None:
    encoded = [base64.urlsafe_b64encode(os.urandom(32)).decode() for _ in range(4)]
    valid = {
        "NORTHSTAR_DATABASE_KEY": encoded[0],
        "NORTHSTAR_DATA_KEY": encoded[1],
        "NORTHSTAR_BACKUP_KEY": encoded[2],
        "NORTHSTAR_BLIND_INDEX_KEY": encoded[3],
        "NORTHSTAR_SESSION_KEY": "synthetic-session-key-that-is-long-enough",
        "NORTHSTAR_KEY_VERSION": "1",
    }
    validate_key_environment(valid)

    for update in (
        {"NORTHSTAR_DATABASE_KEY": ""},
        {"NORTHSTAR_DATA_KEY": "not-base64!"},
        {"NORTHSTAR_BACKUP_KEY": base64.urlsafe_b64encode(b"short").decode()},
        {"NORTHSTAR_BLIND_INDEX_KEY": encoded[0]},
        {"NORTHSTAR_SESSION_KEY": "short"},
        {"NORTHSTAR_KEY_VERSION": "zero"},
    ):
        with pytest.raises(ValueError):
            validate_key_environment(valid | update)
