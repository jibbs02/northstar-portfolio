from __future__ import annotations

import re

from fastapi.testclient import TestClient

from northstar.config import Settings
from northstar.main import create_app

SYNTHETIC_ACCOUNT = "INVENTED-LOCAL-8181"


def csrf(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match is not None
    return match.group(1)


def test_manual_production_onboarding_preview_commit_and_history_workflow(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        accounts_page = client.get("/accounts")
        created = client.post(
            "/accounts",
            data={
                "csrf_token": csrf(accounts_page.text),
                "name": "Invented Local Roth",
                "account_type": "roth_ira",
                "tax_treatment": "tax_free",
                "provider_identifier": SYNTHETIC_ACCOUNT,
                "notes": "Manual onboarding workflow",
            },
            follow_redirects=False,
        )
        account_id = app.state.portfolio.list_accounts()[0].id
        imports_page = client.get("/imports")
        payload = (
            "Account Number,Trade Date,Settlement Date,Action,Symbol,Description,Quantity,"
            "Price,Fees,Amount,Cost Basis\n"
            f"{SYNTHETIC_ACCOUNT},2026-03-01,2026-03-03,BUY,SYNE,Invented holding,"
            "3,4.125,0.01,-12.39,12.38\n"
        ).encode()
        preview = client.post(
            "/imports/preview",
            data={
                "csrf_token": csrf(imports_page.text),
                "adapter_key": "manual_transactions_csv_v1",
                "account_id": account_id,
            },
            files={"upload": ("synthetic-input.csv", payload, "text/csv")},
        )
        action = re.search(r'action="(/imports/[^/]+/commit)"', preview.text)
        assert action is not None
        committed = client.post(
            action.group(1),
            data={"csrf_token": csrf(preview.text)},
            follow_redirects=False,
        )
        history = client.get("/imports")

    assert created.status_code == 303
    assert preview.status_code == 200
    assert "Invented holding" in preview.text
    assert SYNTHETIC_ACCOUNT not in preview.text
    assert "synthetic-input.csv" not in preview.text
    assert committed.status_code == 303
    assert "Committed" in history.text
    assert SYNTHETIC_ACCOUNT not in history.text


def test_onboarding_pages_expose_only_manual_production_csv_workflows(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        accounts = client.get("/accounts")
        client.post(
            "/accounts",
            data={
                "csrf_token": csrf(accounts.text),
                "name": "Test account",
                "account_type": "cash",
                "tax_treatment": "not_applicable",
            },
        )
        imports = client.get("/imports")

    assert accounts.status_code == 200
    assert "approved production path" in accounts.text
    assert "full account number" in accounts.text
    assert imports.status_code == 200
    assert "manual_positions_csv_v1" in imports.text
    assert "manual_transactions_csv_v1" in imports.text
    assert "schwab_transactions_synthetic_v1" not in imports.text
    assert "Preview a synthetic CSV" not in imports.text
    assert "synthetic cash position" not in client.get("/settings").text
