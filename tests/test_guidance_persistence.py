from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from northstar.config import Settings
from northstar.data import (
    Database,
    EnvelopeSetting,
    GuidanceRepository,
    PortfolioRepository,
    PortfolioRules,
    TargetSetting,
)
from northstar.data.database import LATEST_SCHEMA_VERSION, MIGRATION_2, SCHEMA_V1, utc_now
from northstar.services import BackupService, PortfolioImportService

SYNTHETIC_IDENTIFIER = "SYNTHETIC-GUIDANCE-7001"
POSITION_HEADER = (
    "Row Type,Account Number,Symbol,Description,Security Type,Quantity,Price,Market Value,"
    "Cost Basis,As Of Date\n"
)


def _portfolio_stack(
    settings: Settings,
    *,
    account_type: str = "roth_ira",
    tax_treatment: str = "tax_free",
    rows: tuple[str, ...] = (
        "POSITION,SYNTHETIC-GUIDANCE-7001,SYNA,Invented A,ETF,6,10,60,42,2026-08-07",
        "POSITION,SYNTHETIC-GUIDANCE-7001,SYNB,Invented B,ETF,4,10,40,30,2026-08-07",
        "TOTAL,SYNTHETIC-GUIDANCE-7001,,,,,,100,,",
    ),
) -> tuple[Database, PortfolioRepository, GuidanceRepository, str]:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    database.initialize()
    portfolio = PortfolioRepository(database)
    account = portfolio.create_account(
        name="Invented Guidance Account",
        account_type=account_type,
        tax_treatment=tax_treatment,
        provider_identifier=SYNTHETIC_IDENTIFIER,
    )
    service = PortfolioImportService(database, portfolio)
    payload = (POSITION_HEADER + "\n".join(rows) + "\n").encode()
    preview = service.preview("schwab_positions_synthetic_v1", account.id, payload)
    service.commit(preview.batch.id, "synthetic-test-actor")
    return database, portfolio, GuidanceRepository(database), account.id


def test_encrypted_v2_database_migrates_to_guidance_schema(settings: Settings) -> None:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    settings.database_path.parent.mkdir(parents=True)
    with closing(database.connect()) as conn:
        conn.executescript(SCHEMA_V1)
        conn.executescript(MIGRATION_2)
        conn.execute(
            "INSERT INTO app_meta(key, value, updated_at) VALUES('schema_version', '2', ?)",
            (utc_now(),),
        )
        conn.commit()

    database.initialize()
    reopened = database.at_path(settings.database_path)
    reopened.initialize()
    with closing(reopened.connect()) as conn:
        version = conn.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()[0]
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        security_columns = {row[1] for row in conn.execute("PRAGMA table_info(securities)").fetchall()}

    assert version == str(LATEST_SCHEMA_VERSION)
    assert {"account_preferences", "portfolio_rules", "portfolio_valuation_snapshots"} <= tables
    assert "asset_class" in security_columns
    assert reopened.health()
    assert b"SQLite format 3" not in settings.database_path.read_bytes()


def test_account_targets_and_preferences_are_validated_atomically(settings: Settings) -> None:
    database, _, guidance, account_id = _portfolio_stack(settings)
    choices = guidance.security_choices(account_id)
    assert len(choices) == 2
    guidance.save_account_settings(
        account_id,
        [
            TargetSetting(choices[0].id, Decimal("60"), True),
            TargetSetting(choices[1].id, Decimal("40"), False),
        ],
        fractional_shares=True,
        dividend_reinvestment=True,
        actor="synthetic-test-actor",
    )

    with pytest.raises(ValueError, match="exactly 100"):
        guidance.save_account_settings(
            account_id,
            [
                TargetSetting(choices[0].id, Decimal("70"), False),
                TargetSetting(choices[1].id, Decimal("40"), True),
            ],
            fractional_shares=False,
            dividend_reinvestment=False,
            actor="synthetic-test-actor",
        )

    stored_targets = {item.security_id: item.target_percentage for item in guidance.targets(account_id)}
    assert stored_targets == {
        choices[0].id: Decimal("60.000000"),
        choices[1].id: Decimal("40.000000"),
    }
    preference = guidance.account_preference(account_id)
    assert preference.fractional_shares
    assert preference.dividend_reinvestment
    with closing(database.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM account_targets").fetchone()[0] == 2


def test_mixed_legacy_and_current_csv_snapshots_rank_by_source_date(
    settings: Settings,
) -> None:
    old_rows = (
        "POSITION,SYNTHETIC-GUIDANCE-7001,SYNA,Invented A,ETF,1,10,10,8,2025-01-01",
        "TOTAL,SYNTHETIC-GUIDANCE-7001,,,,,,10,,",
    )
    database, portfolio, guidance, account_id = _portfolio_stack(settings, rows=old_rows)
    legacy_ingestion_time = "2026-08-09T15:00:00+00:00"
    with closing(database.connect()) as conn:
        conn.execute("UPDATE position_snapshots SET as_of_utc = ?", (legacy_ingestion_time,))
        conn.execute(
            "UPDATE market_price_snapshots SET observed_at_utc = ?",
            (legacy_ingestion_time,),
        )
        conn.commit()

    current_rows = (
        "POSITION,SYNTHETIC-GUIDANCE-7001,SYNA,Invented A,ETF,2,20,40,30,2026-08-08",
        "TOTAL,SYNTHETIC-GUIDANCE-7001,,,,,,40,,",
    )
    service = PortfolioImportService(database, portfolio)
    payload = (POSITION_HEADER + "\n".join(current_rows) + "\n").encode()
    preview = service.preview("schwab_positions_synthetic_v1", account_id, payload)
    service.commit(preview.batch.id, "synthetic-test-actor")

    fact = next(item for item in guidance.holding_facts() if item.symbol == "SYNA")

    assert fact.quantity == Decimal("2.000000")
    assert fact.snapshot_value == Decimal("40.000000")
    assert fact.price == Decimal("20.000000")
    assert fact.position_as_of == datetime(2026, 8, 8, tzinfo=UTC)
    assert fact.price_as_of == datetime(2026, 8, 8, tzinfo=UTC)


def test_portfolio_guardrails_reject_invalid_update_without_partial_write(
    settings: Settings,
) -> None:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    database.initialize()
    guidance = GuidanceRepository(database)
    original = PortfolioRules(Decimal("5"), Decimal("8"), Decimal("12"), Decimal("4"), 24, 36)
    guidance.save_rules(original, actor="synthetic-test-actor")

    with pytest.raises(ValueError, match="ordered"):
        guidance.save_rules(
            PortfolioRules(Decimal("10"), Decimal("9"), Decimal("20"), Decimal("3"), 48, 48),
            actor="synthetic-test-actor",
        )

    assert guidance.rules() == PortfolioRules(
        Decimal("5.000000"),
        Decimal("8.000000"),
        Decimal("12.000000"),
        Decimal("4.000000"),
        24,
        36,
    )


def test_cash_envelopes_are_encrypted_virtual_assignments(settings: Settings) -> None:
    rows = (
        "POSITION,SYNTHETIC-GUIDANCE-7001,SYNCASH,Invented Cash,CASH,100,1,100,100,2026-08-07",
        "TOTAL,SYNTHETIC-GUIDANCE-7001,,,,,,100,,",
    )
    database, _, guidance, account_id = _portfolio_stack(
        settings,
        account_type="cash",
        tax_treatment="not_applicable",
        rows=rows,
    )
    guidance.save_envelopes(
        account_id,
        [EnvelopeSetting("Invented reserve", Decimal("80"), Decimal("25"))],
        actual_cash=Decimal("100"),
        actor="synthetic-test-actor",
    )

    with pytest.raises(ValueError, match="exceed actual cash"):
        guidance.save_envelopes(
            account_id,
            [EnvelopeSetting("Rejected assignment", None, Decimal("100.01"))],
            actual_cash=Decimal("100"),
            actor="synthetic-test-actor",
        )

    facts = guidance.envelope_facts()
    with closing(database.connect()) as conn:
        envelope = conn.execute("SELECT encrypted_name FROM cash_envelopes").fetchone()[0]
        transaction_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        audit = conn.execute(
            "SELECT metadata_json FROM audit_events WHERE event_type='settings.cash_envelopes_updated'"
        ).fetchone()[0]

    assert [item.name for item in facts] == ["Invented reserve"]
    assert b"Invented reserve" not in envelope
    assert transaction_count == 0
    assert json.loads(audit) == {"envelope_count": 1}
    assert "25" not in audit


def test_guidance_schema_and_settings_survive_encrypted_restore(settings: Settings, tmp_path: Path) -> None:
    database, _, guidance, account_id = _portfolio_stack(settings)
    choices = guidance.security_choices(account_id)
    guidance.save_account_settings(
        account_id,
        [
            TargetSetting(choices[0].id, Decimal("60"), True),
            TargetSetting(choices[1].id, Decimal("40"), True),
        ],
        fractional_shares=True,
        dividend_reinvestment=False,
        actor="synthetic-test-actor",
    )
    guidance.save_rules(
        PortfolioRules(Decimal("5"), Decimal("8"), Decimal("12"), Decimal("4")),
        actor="synthetic-test-actor",
    )
    backup_service = BackupService(database, settings.backup_dir, settings.backup_vault())

    backup = backup_service.create()
    drill = backup_service.restore_drill(backup)
    destination = tmp_path / "isolated-guidance" / "northstar.db"
    backup_service.restore_to(backup, destination)
    restored = database.at_path(destination)
    restored_guidance = GuidanceRepository(restored)

    assert drill.encrypted_records_verified >= 5
    assert restored.health()
    assert len(restored_guidance.targets(account_id)) == 2
    assert restored_guidance.account_preference(account_id).fractional_shares
    assert restored_guidance.rules().bitcoin_review_percentage == Decimal("12.000000")
