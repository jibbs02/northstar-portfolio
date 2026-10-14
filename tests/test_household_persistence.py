from __future__ import annotations

import json
import uuid
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest

from northstar.config import Settings
from northstar.data import (
    BitcoinContributionBand,
    CashRoleSetting,
    Database,
    HouseholdPolicyRepository,
    HouseholdTarget,
    PortfolioRepository,
    SecurityPolicyMapping,
    SinkingFundSetting,
)
from northstar.data.database import utc_now
from northstar.data.portfolio import encrypted_json
from northstar.services import BackupService, PortfolioImportService

POSITION_HEADER = (
    "Row Type,Account Number,Symbol,Description,Security Type,Quantity,Price,Market Value,"
    "Cost Basis,As Of Date\n"
)


def policy_stack(settings: Settings) -> tuple[Database, HouseholdPolicyRepository, str, dict[str, str]]:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    database.initialize()
    portfolio = PortfolioRepository(database)
    account = portfolio.create_account(
        name="Synthetic Taxable",
        account_type="taxable_brokerage",
        tax_treatment="taxable",
        provider_identifier="SYNTHETIC-POLICY-7001",
    )
    rows = (
        "POSITION,SYNTHETIC-POLICY-7001,SYNUS,Synthetic US,ETF,60,10,600,500,2026-08-08",
        "POSITION,SYNTHETIC-POLICY-7001,SYNBTC,Synthetic Bitcoin,ETF,10,10,100,80,2026-08-08",
        "POSITION,SYNTHETIC-POLICY-7001,SYNCASH,Synthetic Cash,CASH,100,1,100,100,2026-08-08",
        "TOTAL,SYNTHETIC-POLICY-7001,,,,,,800,,",
    )
    service = PortfolioImportService(database, portfolio)
    preview = service.preview(
        "schwab_positions_synthetic_v1",
        account.id,
        (POSITION_HEADER + "\n".join(rows) + "\n").encode(),
    )
    service.commit(preview.batch.id, "synthetic-test-actor")
    with closing(database.connect()) as conn:
        cash_id = conn.execute(
            "SELECT id FROM securities WHERE asset_class='cash'"
        ).fetchone()[0]
        noncash_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM securities WHERE asset_class!='cash' ORDER BY symbol_hash"
            ).fetchall()
        ]
    ids = {"cash": cash_id, "us": noncash_ids[0], "bitcoin": noncash_ids[1]}
    return database, HouseholdPolicyRepository(database), account.id, ids


def targets(*, us: str = "50", international: str = "10") -> list[HouseholdTarget]:
    return [
        HouseholdTarget("us_equity", Decimal(us)),
        HouseholdTarget("international_equity", Decimal(international)),
        HouseholdTarget("nominal_treasury", Decimal("15")),
        HouseholdTarget("tips", Decimal("10")),
        HouseholdTarget("investable_cash", Decimal("5")),
        HouseholdTarget("bitcoin", Decimal("10")),
    ]


def bands() -> list[BitcoinContributionBand]:
    return [
        BitcoinContributionBand(Decimal("0"), Decimal("5"), Decimal("50")),
        BitcoinContributionBand(Decimal("5"), Decimal("100"), Decimal("0")),
    ]


def save_valid_policy(
    repository: HouseholdPolicyRepository, account_id: str, ids: dict[str, str]
) -> None:
    repository.save_settings(
        cash_roles=[CashRoleSetting(account_id, ids["cash"], "investable_cash")],
        security_mappings=[
            SecurityPolicyMapping(ids["cash"], "investable_cash", False),
            SecurityPolicyMapping(ids["us"], "us_equity", True),
            SecurityPolicyMapping(ids["bitcoin"], "bitcoin", False),
        ],
        targets=targets(),
        bitcoin_bands=bands(),
        actor="synthetic-test-actor",
    )


def test_household_policy_settings_round_trip_atomically(settings: Settings) -> None:
    database, repository, account_id, ids = policy_stack(settings)
    save_valid_policy(repository, account_id, ids)

    stored = repository.settings()

    assert stored.cash_roles == (
        CashRoleSetting(account_id, ids["cash"], "investable_cash"),
    )
    assert len(stored.security_mappings) == 3
    assert sum((item.target_percentage for item in stored.targets), Decimal("0")) == Decimal(
        "100.000000"
    )
    assert stored.bitcoin_bands[-1].upper_bound_percentage == Decimal("100.000000")
    with closing(database.connect()) as conn:
        audit = conn.execute(
            "SELECT metadata_json FROM audit_events "
            "WHERE event_type='settings.household_policy_updated'"
        ).fetchone()[0]
    assert json.loads(audit) == {
        "bitcoin_band_count": 2,
        "cash_role_count": 1,
        "mapping_count": 3,
        "sinking_fund_configured": False,
        "target_count": 6,
    }
    assert "50.000000" not in audit


def test_invalid_targets_do_not_replace_valid_policy(settings: Settings) -> None:
    _, repository, account_id, ids = policy_stack(settings)
    save_valid_policy(repository, account_id, ids)
    before = repository.settings()

    with pytest.raises(ValueError, match="exactly 100"):
        repository.save_settings(
            cash_roles=before.cash_roles,
            security_mappings=before.security_mappings,
            targets=targets(us="55"),
            bitcoin_bands=before.bitcoin_bands,
            actor="synthetic-test-actor",
        )

    assert repository.settings() == before


def test_bitcoin_bands_reject_gaps_and_cash_roles_reject_non_cash(
    settings: Settings,
) -> None:
    _, repository, account_id, ids = policy_stack(settings)
    with pytest.raises(ValueError, match="without gaps"):
        repository.save_settings(
            cash_roles=[CashRoleSetting(account_id, ids["cash"], "investable_cash")],
            security_mappings=[],
            targets=targets(),
            bitcoin_bands=[
                BitcoinContributionBand(Decimal("0"), Decimal("4"), Decimal("50")),
                BitcoinContributionBand(Decimal("5"), Decimal("100"), Decimal("0")),
            ],
            actor="synthetic-test-actor",
        )


def test_sinking_fund_setting_is_encrypted_and_reference_validated(
    settings: Settings,
) -> None:
    database, repository, account_id, ids = policy_stack(settings)
    envelope_id = str(uuid.uuid4())
    now = utc_now()
    with database.transaction() as conn:
        conn.execute(
            """INSERT INTO cash_envelopes(
                   id, account_id, encrypted_name, target_amount, current_amount,
                   encrypted_notes, key_version, created_at, updated_at
               ) VALUES (?, ?, ?, '500.00', '125.00', NULL, ?, ?, ?)""",
            (
                envelope_id,
                account_id,
                encrypted_json(
                    database, "cash_envelopes", envelope_id, "name", "Synthetic sinking fund"
                ),
                database.vault.key_version,
                now,
                now,
            ),
        )
    repository.save_settings(
        cash_roles=[CashRoleSetting(account_id, ids["cash"], "investable_cash")],
        security_mappings=[
            SecurityPolicyMapping(ids["cash"], "investable_cash", False),
            SecurityPolicyMapping(ids["us"], "us_equity", True),
            SecurityPolicyMapping(ids["bitcoin"], "bitcoin", False),
        ],
        targets=targets(),
        bitcoin_bands=bands(),
        sinking_fund=SinkingFundSetting(envelope_id, Decimal("75")),
        actor="synthetic-test-actor",
    )

    with closing(database.connect()) as conn:
        payload = conn.execute(
            "SELECT encrypted_payload FROM secure_records "
            "WHERE category='household-plan-settings'"
        ).fetchone()[0]
    assert payload.startswith(b"NSV1")
    assert b"75.00" not in payload
    assert repository.settings().sinking_fund == SinkingFundSetting(
        envelope_id, Decimal("75.00")
    )

    previous = repository.settings()
    with pytest.raises(ValueError, match="envelope with a goal"):
        repository.save_settings(
            cash_roles=previous.cash_roles,
            security_mappings=previous.security_mappings,
            targets=previous.targets,
            bitcoin_bands=previous.bitcoin_bands,
            sinking_fund=SinkingFundSetting("missing-envelope", Decimal("100")),
            actor="synthetic-test-actor",
        )
    assert repository.settings() == previous


def test_household_policy_survives_encrypted_backup_and_isolated_restore(
    settings: Settings, tmp_path: Path
) -> None:
    database, repository, account_id, ids = policy_stack(settings)
    save_valid_policy(repository, account_id, ids)
    expected = repository.settings()
    backups = BackupService(database, settings.backup_dir, settings.backup_vault())

    backup = backups.create()
    drill = backups.restore_drill(backup)
    restored_path = tmp_path / "isolated-household-policy" / "northstar.db"
    backups.restore_to(backup, restored_path)
    restored = database.at_path(restored_path)

    assert drill.schema_version > 3
    assert restored.health()
    assert HouseholdPolicyRepository(restored).settings() == expected
    with pytest.raises(ValueError, match="current cash holding"):
        repository.save_settings(
            cash_roles=[CashRoleSetting(account_id, ids["us"], "emergency_reserve")],
            security_mappings=[],
            targets=targets(),
            bitcoin_bands=bands(),
            actor="synthetic-test-actor",
        )
