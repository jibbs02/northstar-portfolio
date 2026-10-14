from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from northstar.config import Settings
from northstar.data import PortfolioRules, TargetSetting
from northstar.main import create_app

SYNTHETIC_IDENTIFIER = "SYNTHETIC-ROUTE-8802"
AS_OF_DATE = date.today().isoformat()
POSITION_PAYLOAD = (
    b"Row Type,Account Number,Symbol,Description,Security Type,Quantity,Price,Market Value,"
    b"Cost Basis,As Of Date\n"
    + f"POSITION,SYNTHETIC-ROUTE-8802,SYNR,Invented Route Fund,ETF,10,10,100,75,{AS_OF_DATE}\n".encode()
    +
    b"TOTAL,SYNTHETIC-ROUTE-8802,,,,,,100,,\n"
)


def _csrf(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match is not None
    return match.group(1)


def _seed_guidance(app: object) -> str:
    portfolio = app.state.portfolio
    account = portfolio.create_account(
        name="Invented <script>alert-safe</script> Account",
        account_type="roth_ira",
        tax_treatment="tax_free",
        provider_identifier=SYNTHETIC_IDENTIFIER,
    )
    imports = app.state.import_service
    preview = imports.preview("schwab_positions_synthetic_v1", account.id, POSITION_PAYLOAD)
    imports.commit(preview.batch.id, "synthetic-route-actor")
    choice = app.state.guidance_repository.security_choices(account.id)[0]
    app.state.guidance_repository.save_account_settings(
        account.id,
        [TargetSetting(choice.id, Decimal("100"), True)],
        fractional_shares=True,
        dividend_reinvestment=False,
        actor="synthetic-route-actor",
    )
    app.state.guidance_repository.save_rules(
        PortfolioRules(Decimal("5"), Decimal("8"), Decimal("12"), Decimal("4")),
        actor="synthetic-route-actor",
    )
    return account.id


def test_dashboard_is_accessible_script_free_and_escapes_sensitive_labels(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        _seed_guidance(app)
        response = client.get("/")

    assert response.status_code == 200
    assert '<a class="skip-link" href="#main-content">' in response.text
    assert '<main id="main-content">' in response.text
    assert "<table" in response.text
    assert 'scope="col"' in response.text
    assert "<script" not in response.text
    assert "localStorage" not in response.text
    assert "sessionStorage" not in response.text
    assert "http://" not in response.text
    assert "https://" not in response.text
    assert "data-" not in response.text
    assert "&lt;script&gt;alert-safe&lt;/script&gt;" in response.text
    assert SYNTHETIC_IDENTIFIER not in response.text
    assert response.headers["Cache-Control"] == "no-store, private, max-age=0"
    assert "Value source: position snapshot" in response.text
    assert "schwab positions synthetic v1" in response.text
    assert AS_OF_DATE in response.text


def test_guidance_mutations_require_csrf_and_errors_do_not_echo_inputs(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        account_id = _seed_guidance(app)
        assert (
            client.post(
                "/settings/portfolio",
                data={
                    "bitcoin_target": "5",
                    "bitcoin_caution": "8",
                    "bitcoin_review": "12",
                    "rebalance_threshold": "4",
                },
            ).status_code
            == 403
        )
        allocation_page = client.get("/allocation")
        token = _csrf(allocation_page.text)
        caplog.set_level(logging.DEBUG)
        invalid = client.post(
            "/allocation",
            data={
                "csrf_token": token,
                "account_id": account_id,
                "additional_cash": "98765.4321-private",
            },
        )

    log_messages = " ".join(record.getMessage() for record in caplog.records)
    assert invalid.status_code == 422
    assert "The allocation could not be calculated" in invalid.text
    assert "98765.4321" not in invalid.text
    assert "98765.4321" not in log_messages
    assert SYNTHETIC_IDENTIFIER not in invalid.text
    assert SYNTHETIC_IDENTIFIER not in log_messages
    assert not invalid.url.query


def test_synthetic_contribution_allocator_renders_planning_output(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        account_id = _seed_guidance(app)
        token = _csrf(client.get("/allocation").text)
        response = client.post(
            "/allocation",
            data={
                "csrf_token": token,
                "account_id": account_id,
                "additional_cash": "25.00",
            },
        )

    assert response.status_code == 200
    assert "PLANNING RECOMMENDATION" in response.text
    assert "planning only" in response.text
    assert "Recommended dollars" in response.text
    assert SYNTHETIC_IDENTIFIER not in response.text


def test_cloudflare_production_mode_denies_an_unauthenticated_dashboard(
    settings: Settings, tmp_path: Path
) -> None:
    runtime = tmp_path / "production-runtime"
    backups = runtime / "backups"
    runtime.mkdir(mode=0o700)
    backups.mkdir(mode=0o700)
    runtime.chmod(0o700)
    backups.chmod(0o700)
    secrets = tmp_path / "synthetic-secrets.env"
    secrets.write_text("synthetic-only=true\n")
    secrets.chmod(0o600)
    production = Settings(
        environment="production",
        auth_mode="cloudflare",
        database_path=runtime / "northstar.db",
        backup_dir=backups,
        secrets_path=secrets,
        database_key=settings.database_key,
        data_key=settings.data_key,
        backup_key=settings.backup_key,
        blind_index_key=settings.blind_index_key,
        session_key=settings.session_key,
        key_version=1,
        backup_daily_retention=14,
        backup_monthly_retention=12,
        cf_issuer="https://synthetic-team.cloudflareaccess.com",
        cf_audience="synthetic-audience",
        allowed_email="synthetic-user@northstar.invalid",
        secure_cookies=True,
    )
    production.validate()

    with TestClient(create_app(production)) as client:
        health = client.get("/healthz", headers={"Host": "127.0.0.1:8000"})
        denied = client.get("/", headers={"Host": "northstar.example.com"})

    assert health.status_code == 200
    assert denied.status_code == 401
    assert denied.json() == {"error": "Authentication required"}
