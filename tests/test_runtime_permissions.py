from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from northstar.config import Settings
from northstar.main import create_app


def production_settings(settings: Settings, tmp_path: Path) -> Settings:
    secrets_path = tmp_path / "runtime" / "secrets.env"
    secrets_path.parent.mkdir(mode=0o700)
    secrets_path.write_text("synthetic placeholders only\n", encoding="utf-8")
    secrets_path.chmod(0o600)
    settings.database_path.parent.mkdir(parents=True, mode=0o700)
    settings.database_path.parent.chmod(0o700)
    settings.backup_dir.mkdir(parents=True, mode=0o700)
    settings.backup_dir.chmod(0o700)
    return replace(
        settings,
        environment="production",
        auth_mode="cloudflare",
        secrets_path=secrets_path,
        cf_issuer="https://synthetic-team.cloudflareaccess.com",
        cf_audience="synthetic-audience",
        allowed_email="synthetic@example.invalid",
        secure_cookies=True,
    )


def test_production_accepts_owner_only_runtime_permissions(settings: Settings, tmp_path: Path) -> None:
    production = production_settings(settings, tmp_path)

    production.validate_runtime_permissions()


def test_production_startup_rejects_group_readable_secrets(settings: Settings, tmp_path: Path) -> None:
    production = production_settings(settings, tmp_path)
    assert production.secrets_path is not None
    production.secrets_path.chmod(0o640)

    with pytest.raises(ValueError, match="group or other"):
        with TestClient(create_app(production)):
            pass


def test_production_startup_rejects_open_runtime_directory(settings: Settings, tmp_path: Path) -> None:
    production = production_settings(settings, tmp_path)
    production.backup_dir.chmod(0o755)

    with pytest.raises(ValueError, match="Backup directory"):
        with TestClient(create_app(production)):
            pass
