from __future__ import annotations

import os
from pathlib import Path

import pytest

from northstar.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        auth_mode="local",
        database_path=tmp_path / "data" / "northstar.db",
        backup_dir=tmp_path / "backups",
        secrets_path=None,
        database_key=os.urandom(32),
        data_key=os.urandom(32),
        backup_key=os.urandom(32),
        blind_index_key=os.urandom(32),
        session_key="test-session-key-000000000000000000000000000000",
        key_version=1,
        backup_daily_retention=14,
        backup_monthly_retention=12,
        cf_issuer=None,
        cf_audience=None,
        allowed_email=None,
        secure_cookies=False,
    )
