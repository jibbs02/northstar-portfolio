from __future__ import annotations

import re
from contextlib import closing
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from northstar.config import Settings
from northstar.data import (
    BitcoinContributionBand,
    HouseholdTarget,
    SecurityPolicyMapping,
)
from northstar.main import create_app

SYNTHETIC_IDENTIFIER = "SYNTHETIC-PLAN-4410"
AS_OF_DATE = date.today().isoformat()
POSITION_PAYLOAD = (
    b"Row Type,Account Number,Symbol,Description,Security Type,Quantity,Price,Market Value,"
    b"Cost Basis,As Of Date\n"
    + f"POSITION,SYNTHETIC-PLAN-4410,SYNPLAN,Synthetic Plan Fund,ETF,10,10,100,75,{AS_OF_DATE}\n".encode()
    +
    b"TOTAL,SYNTHETIC-PLAN-4410,,,,,,100,,\n"
)
ADDITIONAL_POSITION_PAYLOAD = (
    b"Row Type,Account Number,Symbol,Description,Security Type,Quantity,Price,Market Value,"
    b"Cost Basis,As Of Date\n"
    + f"POSITION,SYNTHETIC-PLAN-4410,SYNPLAN,Synthetic Plan Fund,ETF,10,10,100,75,{AS_OF_DATE}\n".encode()
    + f"POSITION,SYNTHETIC-PLAN-4410,SYNNEW,Synthetic New Holding,ETF,1,10,10,8,{AS_OF_DATE}\n".encode()
    +
    b"TOTAL,SYNTHETIC-PLAN-4410,,,,,,110,,\n"
)


def csrf(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match is not None
    return match.group(1)


def seed_holdings(app: object, payload: bytes = POSITION_PAYLOAD) -> tuple[str, str]:
    account = app.state.portfolio.create_account(
        name="Synthetic Taxable Plan Account",
        account_type="taxable_brokerage",
        tax_treatment="taxable",
        provider_identifier=SYNTHETIC_IDENTIFIER,
    )
    preview = app.state.import_service.preview(
        "schwab_positions_synthetic_v1", account.id, payload
    )
    app.state.import_service.commit(preview.batch.id, "synthetic-plan-actor")
    security = app.state.guidance_repository.security_choices(account.id)[0]
    return account.id, security.id


def seed_policy(app: object, payload: bytes = POSITION_PAYLOAD) -> str:
    _, security_id = seed_holdings(app, payload)
    app.state.household_policy_repository.save_settings(
        cash_roles=[],
        security_mappings=[SecurityPolicyMapping(security_id, "us_equity", True)],
        targets=[
            HouseholdTarget("us_equity", Decimal("100")),
            HouseholdTarget("international_equity", Decimal("0")),
            HouseholdTarget("nominal_treasury", Decimal("0")),
            HouseholdTarget("tips", Decimal("0")),
            HouseholdTarget("investable_cash", Decimal("0")),
            HouseholdTarget("bitcoin", Decimal("0")),
        ],
        bitcoin_bands=[
            BitcoinContributionBand(Decimal("0"), Decimal("100"), Decimal("0"))
        ],
        actor="synthetic-plan-actor",
    )
    return security_id


def test_plan_page_renders_named_denominators_and_transient_checklist(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        seed_policy(app)
        page = client.get("/plan")
        with closing(app.state.database.connect()) as conn:
            audits_before = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        response = client.post(
            "/plan",
            data={
                "csrf_token": csrf(page.text),
                "available_surplus": "25.00",
                "one_time_inflow": "5.00",
            },
        )
        with closing(app.state.database.connect()) as conn:
            audits_after = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
            recommendation_tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name LIKE '%recommendation%'"
            ).fetchone()[0]

    assert page.status_code == 200
    assert "Investment allocation" in page.text
    assert "Household risk exposure" in page.text
    assert '<a class="skip-link" href="#main-content">' in page.text
    assert '<main id="main-content">' in page.text
    assert "http://" not in page.text
    assert "https://" not in page.text
    assert response.status_code == 200
    assert "Ordered action checklist" in response.text
    assert "Direct taxable funds to SYNPLAN" in response.text
    assert "$30.00" in response.text
    assert "planning guidance only" in response.text
    assert SYNTHETIC_IDENTIFIER not in response.text
    assert audits_after == audits_before
    assert recommendation_tables == 0
    with TestClient(app) as client:
        dashboard = client.get("/")
    assert "Investment-allocation share" in dashboard.text
    assert "Household risk exposure share" in dashboard.text
    assert "Consolidated portfolio share" in dashboard.text


def test_application_exposes_no_order_or_money_movement_route(settings: Settings) -> None:
    app = create_app(settings)
    paths = {route.path.lower() for route in app.routes}

    assert all(
        forbidden not in path
        for path in paths
        for forbidden in ("order", "trade", "transfer", "withdraw", "payment")
    )


def test_plan_post_requires_csrf_and_does_not_echo_invalid_amount(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        seed_policy(app)
        denied = client.post("/plan", data={"available_surplus": "25.00"})
        page = client.get("/plan")
        invalid = client.post(
            "/plan",
            data={
                "csrf_token": csrf(page.text),
                "available_surplus": "99887.22-private",
            },
        )

    assert denied.status_code == 403
    assert invalid.status_code == 422
    assert "99887.22" not in invalid.text
    assert not invalid.url.query


def test_old_source_date_import_blocks_plan_even_when_imported_now(settings: Settings) -> None:
    app = create_app(settings)
    old_payload = POSITION_PAYLOAD.replace(AS_OF_DATE.encode(), b"2025-01-01")
    with TestClient(app) as client:
        seed_policy(app, old_payload)
        page = client.get("/plan")
        response = client.post(
            "/plan",
            data={"csrf_token": csrf(page.text), "available_surplus": "25.00"},
        )

    assert response.status_code == 200
    assert "No checklist was produced" in response.text
    assert "Stale Household Data" in response.text
    assert "Direct taxable funds" not in response.text


def test_new_unmapped_security_defaults_to_unclassified_on_complete_save(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        mapped_security_id = seed_policy(app)
        account_id = app.state.portfolio.list_accounts()[0].id
        preview = app.state.import_service.preview(
            "schwab_positions_synthetic_v1", account_id, ADDITIONAL_POSITION_PAYLOAD
        )
        app.state.import_service.commit(preview.batch.id, "synthetic-plan-actor")
        choices = app.state.guidance_repository.security_choices(account_id)
        new_security_id = next(item.id for item in choices if item.symbol == "SYNNEW")
        page = client.get("/settings")
        assert re.search(
            rf'id="policy-sleeve-{new_security_id}"[^>]*>.*?'
            r'<option value="unclassified" selected>',
            page.text,
        )
        response = client.post(
            "/settings/household",
            data={
                "csrf_token": csrf(page.text),
                f"policy_sleeve_{mapped_security_id}": "us_equity",
                f"taxable_eligible_{mapped_security_id}": "yes",
                f"policy_sleeve_{new_security_id}": "unclassified",
                "household_target_us_equity": "100",
                "household_target_international_equity": "0",
                "household_target_nominal_treasury": "0",
                "household_target_tips": "0",
                "household_target_investable_cash": "0",
                "household_target_bitcoin": "0",
                "bitcoin_band_lower": ["0"],
                "bitcoin_band_upper": ["100"],
                "bitcoin_band_contribution": ["0"],
                "sinking_fund_envelope_id": "",
                "sinking_fund_contribution": "",
            },
            follow_redirects=True,
        )

    mappings = {
        item.security_id: item.sleeve
        for item in app.state.household_policy_repository.settings().security_mappings
    }
    assert response.status_code == 200
    assert mappings[new_security_id] == "unclassified"


def test_household_policy_can_be_replaced_through_authenticated_settings_ui(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        security_id = seed_policy(app)
        page = client.get("/settings")
        response = client.post(
            "/settings/household",
            data={
                "csrf_token": csrf(page.text),
                f"policy_sleeve_{security_id}": "us_equity",
                f"taxable_eligible_{security_id}": "yes",
                "household_target_us_equity": "90",
                "household_target_international_equity": "5",
                "household_target_nominal_treasury": "5",
                "household_target_tips": "0",
                "household_target_investable_cash": "0",
                "household_target_bitcoin": "0",
                "bitcoin_band_lower": ["0"],
                "bitcoin_band_upper": ["100"],
                "bitcoin_band_contribution": ["0"],
                "sinking_fund_envelope_id": "",
                "sinking_fund_contribution": "",
            },
            follow_redirects=True,
        )

    stored = app.state.household_policy_repository.settings()
    assert page.status_code == 200
    assert "Household allocation and surplus rules" in page.text
    assert response.status_code == 200
    assert "Settings saved atomically" in response.text
    assert {item.sleeve: item.target_percentage for item in stored.targets}["us_equity"] == Decimal(
        "90.000000"
    )


def test_initial_settings_support_more_than_three_bitcoin_bands(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        _, security_id = seed_holdings(app)
        page = client.get("/settings")
        assert page.text.count('name="bitcoin_band_lower"') == 5
        expanded = client.get("/settings?band_rows=20")
        assert expanded.text.count('name="bitcoin_band_lower"') == 20
        assert "bitcoin_band" not in str(expanded.url.query)
        response = client.post(
            "/settings/household",
            data={
                "csrf_token": csrf(page.text),
                f"policy_sleeve_{security_id}": "us_equity",
                f"taxable_eligible_{security_id}": "yes",
                "household_target_us_equity": "100",
                "household_target_international_equity": "0",
                "household_target_nominal_treasury": "0",
                "household_target_tips": "0",
                "household_target_investable_cash": "0",
                "household_target_bitcoin": "0",
                "bitcoin_band_lower": ["0", "5", "10", "20"],
                "bitcoin_band_upper": ["5", "10", "20", "100"],
                "bitcoin_band_contribution": ["50", "25", "10", "0"],
                "sinking_fund_envelope_id": "",
                "sinking_fund_contribution": "",
            },
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert len(app.state.household_policy_repository.settings().bitcoin_bands) == 4
