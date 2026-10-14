from __future__ import annotations

from contextlib import closing
from decimal import Decimal

import pytest

from northstar.config import Settings
from northstar.data import Database, PortfolioRepository
from northstar.data.database import LATEST_SCHEMA_VERSION, SCHEMA_V1, utc_now
from northstar.domain import DecimalKind, canonical_decimal


def test_encrypted_v1_database_migrates_and_reopens_at_latest_schema(settings: Settings) -> None:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    settings.database_path.parent.mkdir(parents=True)
    with closing(database.connect()) as conn:
        conn.executescript(SCHEMA_V1)
        conn.execute(
            "INSERT INTO app_meta(key, value, updated_at) VALUES('schema_version', '1', ?)",
            (utc_now(),),
        )
        conn.commit()

    database.initialize()
    reopened = Database(settings.database_path, settings.data_vault(), settings.database_key)
    reopened.initialize()
    with closing(reopened.connect()) as conn:
        version = conn.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()[0]
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

    assert version == str(LATEST_SCHEMA_VERSION)
    assert {
        "institutions",
        "providers",
        "accounts",
        "securities",
        "transactions",
        "tax_lots",
        "position_snapshots",
        "market_price_snapshots",
        "account_targets",
        "cash_envelopes",
        "import_batches",
        "audit_events",
        "cash_roles",
        "security_policy_mappings",
        "household_policy_targets",
        "bitcoin_contribution_bands",
        "provider_connections",
        "provider_account_mappings",
        "provider_sync_batches",
    } <= tables
    assert b"SQLite format 3" not in settings.database_path.read_bytes()


@pytest.mark.parametrize(
    ("value", "kind", "expected"),
    [
        ("1.005", DecimalKind.AMOUNT, "1.00"),
        ("1.015", DecimalKind.AMOUNT, "1.02"),
        ("12.123456785", DecimalKind.PRICE, "12.12345678"),
        ("12.123456795", DecimalKind.PRICE, "12.12345680"),
        ("0.1234567890125", DecimalKind.QUANTITY, "0.123456789012"),
        (Decimal("25.1234565"), DecimalKind.PERCENT, "25.123456"),
    ],
)
def test_decimal_precision_uses_fixed_scale_half_even(
    value: str | Decimal, kind: DecimalKind, expected: str
) -> None:
    assert canonical_decimal(value, kind) == expected


def test_binary_float_and_nonfinite_values_are_rejected() -> None:
    with pytest.raises(TypeError):
        canonical_decimal(1.2, DecimalKind.AMOUNT)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite"):
        canonical_decimal("NaN", DecimalKind.PRICE)
    with pytest.raises(ValueError, match="negative"):
        canonical_decimal("-0.01", DecimalKind.QUANTITY, allow_negative=False)


def test_account_sensitive_fields_are_field_encrypted(settings: Settings) -> None:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    database.initialize()
    repository = PortfolioRepository(database)
    account = repository.create_account(
        name="Invented Retirement",
        account_type="roth_ira",
        tax_treatment="tax_free",
        provider_identifier="SYNTHETIC-00991",
        notes="Invented note only",
    )

    with closing(database.connect()) as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account.id,)).fetchone()

    assert row["encrypted_name"].startswith(b"NSV1")
    assert row["encrypted_provider_identifier"].startswith(b"NSV1")
    assert row["encrypted_notes"].startswith(b"NSV1")
    assert row["provider_identifier_hash"] != "SYNTHETIC-00991"
    assert repository.get_account(account.id).name == "Invented Retirement"


def test_portfolio_schema_has_no_binary_floating_point_columns(settings: Settings) -> None:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    database.initialize()
    with closing(database.connect()) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        declared_types = {
            column[2].upper()
            for table in tables
            for column in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        }

    assert not {"REAL", "FLOAT", "DOUBLE"} & declared_types


def test_phase_4c_migration_contains_no_household_policy_defaults(settings: Settings) -> None:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    database.initialize()

    with closing(database.connect()) as conn:
        counts = {
            "cash_roles": conn.execute("SELECT COUNT(*) FROM cash_roles").fetchone()[0],
            "security_policy_mappings": conn.execute(
                "SELECT COUNT(*) FROM security_policy_mappings"
            ).fetchone()[0],
            "household_policy_targets": conn.execute(
                "SELECT COUNT(*) FROM household_policy_targets"
            ).fetchone()[0],
            "bitcoin_contribution_bands": conn.execute(
                "SELECT COUNT(*) FROM bitcoin_contribution_bands"
            ).fetchone()[0],
        }

    assert counts == {
        "cash_roles": 0,
        "security_policy_mappings": 0,
        "household_policy_targets": 0,
        "bitcoin_contribution_bands": 0,
    }
