from __future__ import annotations

import base64
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from northstar.crypto import Vault
from northstar.data import Database
from northstar.operations.config import KEY_NAMES, load_secrets_environment, validate_key_environment
from northstar.services import BackupService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
runtime_creator = runpy.run_path(str(PROJECT_ROOT / "scripts/create_production_runtime.py"))
create_runtime = runtime_creator["_create"]
validate_runtime = runtime_creator["_validate_existing"]
validate_root_boundary = runtime_creator["_validate_root_boundary"]


def _create_synthetic_runtime(root: Path) -> Path:
    secrets_path = root / "secrets.env"
    create_runtime(
        root,
        secrets_path,
        "https://synthetic-team.cloudflareaccess.com",
        "synthetic-audience",
        "synthetic-user@example.invalid",
    )
    return secrets_path


def _vault(value: str, blind_index: str) -> Vault:
    return Vault(
        base64.urlsafe_b64decode(value),
        base64.urlsafe_b64decode(blind_index),
        1,
    )


def _write_reconstructed(path: Path, values: dict[str, str]) -> None:
    recovery_names = (*KEY_NAMES, "NORTHSTAR_SESSION_KEY", "NORTHSTAR_KEY_VERSION")
    path.write_text(
        "".join(f"{name}={values[name]}\n" for name in recovery_names),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_production_runtime_has_independent_owner_only_secrets_and_is_not_overwritten(
    tmp_path: Path,
) -> None:
    root = tmp_path / "final-runtime"
    secrets_path = _create_synthetic_runtime(root)
    original = secrets_path.read_bytes()
    values = load_secrets_environment(secrets_path)

    validate_key_environment(values)
    validate_runtime(root, secrets_path)
    assert secrets_path.stat().st_mode & 0o777 == 0o600
    assert all((root / name).stat().st_mode & 0o777 == 0o700 for name in ("data", "backups", "operations"))
    assert len({values[name] for name in KEY_NAMES}) == len(KEY_NAMES)
    assert values["NORTHSTAR_SCHWAB_ENABLED"] == "0"
    assert values["SCHWAB_CLIENT_ID"] == ""
    assert values["SCHWAB_CLIENT_SECRET"] == ""
    assert values["SCHWAB_REDIRECT_URI"] == ""

    with pytest.raises(FileExistsError):
        create_runtime(
            root,
            secrets_path,
            "https://synthetic-team.cloudflareaccess.com",
            "synthetic-audience",
            "synthetic-user@example.invalid",
        )
    assert secrets_path.read_bytes() == original


def test_production_runtime_rejects_repository_and_symlink_roots(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside the repository"):
        validate_root_boundary(PROJECT_ROOT / "synthetic-runtime")

    real_root = tmp_path / "real"
    real_root.mkdir(mode=0o700)
    link = tmp_path / "runtime-link"
    link.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe"):
        validate_root_boundary(link)


def test_existing_runtime_rejects_nonloopback_and_unapproved_replication_path(tmp_path: Path) -> None:
    root = tmp_path / "production"
    secrets_path = _create_synthetic_runtime(root)
    original = secrets_path.read_text(encoding="utf-8")

    secrets_path.write_text(
        original.replace("NORTHSTAR_BIND_ADDRESS=127.0.0.1", "NORTHSTAR_BIND_ADDRESS=0.0.0.0"),
        encoding="utf-8",
    )
    secrets_path.chmod(0o600)
    with pytest.raises(ValueError, match="bind address"):
        validate_runtime(root, secrets_path)

    secrets_path.write_text(
        original + "NORTHSTAR_REPLICATION_DIR=/tmp/unapproved-recovery\n",
        encoding="utf-8",
    )
    secrets_path.chmod(0o600)
    with pytest.raises(ValueError, match="replication path"):
        validate_runtime(root, secrets_path)


def test_reconstructed_keys_authenticate_backup_in_isolation_and_are_deleted(tmp_path: Path) -> None:
    root = tmp_path / "production"
    secrets_path = _create_synthetic_runtime(root)
    values = load_secrets_environment(secrets_path)
    database_path = Path(values["NORTHSTAR_DATABASE_PATH"])
    database = Database(
        database_path,
        _vault(values["NORTHSTAR_DATA_KEY"], values["NORTHSTAR_BLIND_INDEX_KEY"]),
        base64.urlsafe_b64decode(values["NORTHSTAR_DATABASE_KEY"]),
    )
    database.initialize()
    database.upsert_secure_record("checkpoint", "synthetic", {"value": "invented"})
    backups = BackupService(
        database,
        Path(values["NORTHSTAR_BACKUP_DIR"]),
        _vault(values["NORTHSTAR_BACKUP_KEY"], values["NORTHSTAR_BLIND_INDEX_KEY"]),
    )
    backups.create()
    database_before = database_path.read_bytes()
    reconstructed = tmp_path / "reconstructed.env"
    _write_reconstructed(reconstructed, values)

    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts/reconstructed_key_drill.py"),
            "--production-secrets",
            str(secrets_path),
            "--reconstructed-secrets",
            str(reconstructed),
            "--delete-reconstructed",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
    )

    assert result.returncode == 0
    assert '"status":"ok"' in result.stdout
    assert "invented" not in result.stdout
    assert not reconstructed.exists()
    assert database_path.read_bytes() == database_before
    assert database.get_secure_record("checkpoint", "synthetic") == {"value": "invented"}


def test_mismatched_reconstructed_keys_fail_closed_and_are_deleted(tmp_path: Path) -> None:
    root = tmp_path / "production"
    secrets_path = _create_synthetic_runtime(root)
    values = load_secrets_environment(secrets_path)
    reconstructed = tmp_path / "reconstructed.env"
    altered = dict(values)
    altered["NORTHSTAR_DATABASE_KEY"] = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    _write_reconstructed(reconstructed, altered)

    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts/reconstructed_key_drill.py"),
            "--production-secrets",
            str(secrets_path),
            "--reconstructed-secrets",
            str(reconstructed),
            "--delete-reconstructed",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
    )

    assert result.returncode == 1
    assert result.stdout.strip() == '{"status": "fail", "code": "reconstructed_key_drill_failed"}'
    assert not reconstructed.exists()
