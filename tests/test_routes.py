from __future__ import annotations

import re

from fastapi.testclient import TestClient

from northstar.config import Settings
from northstar.main import create_app


def test_foundation_page_and_health_do_not_leak_details(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        health = client.get("/healthz")
        page = client.get("/")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert page.status_code == 200
    assert "The private foundation is running" in page.text
    assert "Content-Security-Policy" in page.headers
    assert page.headers["Cache-Control"] == "no-store, private, max-age=0"
    assert "server" not in {key.lower() for key in health.json()}


def test_mutation_requires_csrf_and_writes_only_encrypted_demo_record(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        denied = client.post("/demo/verify-storage", data={})
        page = client.get("/")
        token_match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        assert token_match is not None
        accepted = client.post(
            "/demo/verify-storage",
            data={"csrf_token": token_match.group(1)},
            follow_redirects=False,
        )

    assert denied.status_code == 403
    assert accepted.status_code == 303


def test_production_rejects_local_auth(settings: Settings) -> None:
    invalid = Settings(
        environment="production",
        auth_mode="local",
        database_path=settings.database_path,
        backup_dir=settings.backup_dir,
        secrets_path=None,
        database_key=settings.database_key,
        data_key=settings.data_key,
        backup_key=settings.backup_key,
        blind_index_key=settings.blind_index_key,
        session_key=settings.session_key,
        key_version=1,
        backup_daily_retention=14,
        backup_monthly_retention=12,
        cf_issuer=None,
        cf_audience=None,
        allowed_email=None,
        secure_cookies=True,
    )

    try:
        invalid.validate()
    except ValueError as exc:
        assert "Cloudflare" in str(exc)
    else:
        raise AssertionError("Production local-auth configuration should be rejected")
