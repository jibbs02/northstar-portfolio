from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlcipher3 import dbapi2 as sqlite3

from northstar.crypto import Vault

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS secure_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    lookup_hash TEXT NOT NULL,
    encrypted_payload BLOB NOT NULL,
    key_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(category, lookup_hash)
);

CREATE TABLE IF NOT EXISTS oauth_transactions (
    state_hash TEXT PRIMARY KEY NOT NULL,
    session_hash TEXT NOT NULL,
    encrypted_verifier BLOB,
    key_version INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL,
    records_seen INTEGER NOT NULL DEFAULT 0,
    records_written INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    actor_hash TEXT NOT NULL,
    subject_hash TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS secure_records_category_idx ON secure_records(category);
CREATE INDEX IF NOT EXISTS sync_runs_started_idx ON sync_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_created_idx ON audit_events(created_at DESC);
"""

LATEST_SCHEMA_VERSION = 11

MIGRATION_2 = """
CREATE TABLE institutions (
    id TEXT PRIMARY KEY NOT NULL,
    encrypted_name BLOB NOT NULL,
    key_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE providers (
    id TEXT PRIMARY KEY NOT NULL,
    institution_id TEXT REFERENCES institutions(id),
    provider_type TEXT NOT NULL,
    encrypted_name BLOB NOT NULL,
    encrypted_identifier BLOB,
    identifier_hash TEXT UNIQUE,
    key_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE accounts (
    id TEXT PRIMARY KEY NOT NULL,
    provider_id TEXT REFERENCES providers(id),
    account_type TEXT NOT NULL CHECK(account_type IN
        ('roth_ira', 'hsa', 'taxable_brokerage', 'cash', 'bitcoin')),
    tax_treatment TEXT NOT NULL CHECK(tax_treatment IN
        ('tax_free', 'tax_advantaged', 'taxable', 'not_applicable')),
    currency TEXT NOT NULL DEFAULT 'USD',
    encrypted_name BLOB NOT NULL,
    encrypted_provider_identifier BLOB,
    provider_identifier_hash TEXT UNIQUE,
    encrypted_notes BLOB,
    key_version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'closed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE securities (
    id TEXT PRIMARY KEY NOT NULL,
    instrument_type TEXT NOT NULL CHECK(instrument_type IN
        ('equity', 'etf', 'mutual_fund', 'bond', 'cash', 'bitcoin', 'other')),
    currency TEXT NOT NULL DEFAULT 'USD',
    encrypted_symbol BLOB,
    symbol_hash TEXT UNIQUE,
    encrypted_name BLOB,
    key_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE import_batches (
    id TEXT PRIMARY KEY NOT NULL,
    adapter_key TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('preview', 'committed', 'failed', 'reversed')),
    file_size_bytes INTEGER NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    accepted_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    reconciliation_status TEXT NOT NULL CHECK(reconciliation_status IN
        ('not_provided', 'matched', 'mismatched', 'not_applicable')),
    encrypted_summary BLOB,
    key_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    imported_at TEXT,
    reversed_at TEXT,
    UNIQUE(adapter_key, account_id, fingerprint)
);

CREATE TABLE import_previews (
    batch_id TEXT PRIMARY KEY NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    encrypted_payload BLOB NOT NULL,
    key_version INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE transactions (
    id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    security_id TEXT REFERENCES securities(id),
    transaction_type TEXT NOT NULL CHECK(transaction_type IN
        ('buy', 'sell', 'dividend', 'reinvestment', 'interest', 'deposit',
         'withdrawal', 'transfer_in', 'transfer_out', 'fee', 'adjustment')),
    trade_date TEXT NOT NULL,
    settlement_date TEXT,
    imported_at TEXT NOT NULL,
    quantity TEXT,
    price TEXT,
    gross_amount TEXT,
    gross_amount_status TEXT NOT NULL CHECK(gross_amount_status IN
        ('supplied', 'unknown', 'not_applicable')),
    fees TEXT,
    net_amount TEXT,
    net_amount_status TEXT NOT NULL CHECK(net_amount_status IN
        ('supplied', 'unknown', 'not_applicable')),
    cost_basis TEXT,
    cost_basis_status TEXT NOT NULL CHECK(cost_basis_status IN
        ('supplied', 'unknown', 'not_applicable')),
    fees_status TEXT NOT NULL CHECK(fees_status IN ('supplied', 'unknown', 'not_applicable')),
    encrypted_description BLOB,
    encrypted_notes BLOB,
    source_record_hash TEXT NOT NULL,
    import_batch_id TEXT REFERENCES import_batches(id),
    key_version INTEGER NOT NULL,
    reversed_at TEXT,
    UNIQUE(account_id, source_record_hash)
);

CREATE TABLE tax_lots (
    id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    security_id TEXT NOT NULL REFERENCES securities(id),
    acquired_date TEXT NOT NULL,
    quantity TEXT NOT NULL,
    cost_basis TEXT,
    cost_basis_status TEXT NOT NULL CHECK(cost_basis_status IN ('supplied', 'unknown')),
    import_batch_id TEXT REFERENCES import_batches(id),
    reversed_at TEXT
);

CREATE TABLE position_snapshots (
    id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    security_id TEXT NOT NULL REFERENCES securities(id),
    as_of_utc TEXT NOT NULL,
    source_date TEXT NOT NULL,
    quantity TEXT NOT NULL,
    market_value TEXT,
    market_value_status TEXT NOT NULL CHECK(market_value_status IN ('supplied', 'unknown')),
    cost_basis TEXT,
    cost_basis_status TEXT NOT NULL CHECK(cost_basis_status IN ('supplied', 'unknown')),
    import_batch_id TEXT REFERENCES import_batches(id),
    reversed_at TEXT
);

CREATE TABLE market_price_snapshots (
    id TEXT PRIMARY KEY NOT NULL,
    security_id TEXT NOT NULL REFERENCES securities(id),
    observed_at_utc TEXT NOT NULL,
    source_date TEXT NOT NULL,
    price TEXT NOT NULL,
    source TEXT NOT NULL,
    import_batch_id TEXT REFERENCES import_batches(id),
    reversed_at TEXT
);

CREATE TABLE account_targets (
    id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    security_id TEXT REFERENCES securities(id),
    target_type TEXT NOT NULL CHECK(target_type IN ('percentage', 'amount')),
    target_percentage TEXT,
    target_amount TEXT,
    encrypted_notes BLOB,
    key_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((target_type='percentage' AND target_percentage IS NOT NULL AND target_amount IS NULL)
       OR (target_type='amount' AND target_amount IS NOT NULL AND target_percentage IS NULL))
);

CREATE TABLE cash_envelopes (
    id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT REFERENCES accounts(id),
    encrypted_name BLOB NOT NULL,
    target_amount TEXT,
    current_amount TEXT,
    encrypted_notes BLOB,
    key_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE import_batch_records (
    batch_id TEXT NOT NULL REFERENCES import_batches(id),
    record_type TEXT NOT NULL CHECK(record_type IN
        ('transaction', 'tax_lot', 'position_snapshot', 'market_price_snapshot')),
    record_id TEXT NOT NULL,
    PRIMARY KEY(batch_id, record_type, record_id)
);

CREATE INDEX accounts_provider_idx ON accounts(provider_id);
CREATE INDEX transactions_account_trade_idx ON transactions(account_id, trade_date DESC);
CREATE INDEX transactions_batch_idx ON transactions(import_batch_id);
CREATE INDEX tax_lots_account_security_idx ON tax_lots(account_id, security_id);
CREATE INDEX positions_account_date_idx ON position_snapshots(account_id, source_date DESC);
CREATE INDEX prices_security_date_idx ON market_price_snapshots(security_id, source_date DESC);
CREATE INDEX import_batches_created_idx ON import_batches(created_at DESC);
"""

MIGRATION_3 = """
ALTER TABLE securities ADD COLUMN asset_class TEXT NOT NULL DEFAULT 'other'
    CHECK(asset_class IN ('equity', 'fixed_income', 'cash', 'bitcoin', 'other'));

UPDATE securities SET asset_class = CASE
    WHEN instrument_type IN ('equity', 'etf', 'mutual_fund') THEN 'equity'
    WHEN instrument_type IN ('bond') THEN 'fixed_income'
    WHEN instrument_type = 'cash' THEN 'cash'
    WHEN instrument_type = 'bitcoin' THEN 'bitcoin'
    ELSE 'other'
END;

ALTER TABLE account_targets ADD COLUMN eligible_for_contributions INTEGER NOT NULL DEFAULT 1
    CHECK(eligible_for_contributions IN (0, 1));

CREATE UNIQUE INDEX account_targets_account_security_unique
    ON account_targets(account_id, security_id) WHERE security_id IS NOT NULL;

CREATE TABLE account_preferences (
    account_id TEXT PRIMARY KEY NOT NULL REFERENCES accounts(id),
    fractional_shares INTEGER NOT NULL DEFAULT 0 CHECK(fractional_shares IN (0, 1)),
    dividend_reinvestment INTEGER NOT NULL DEFAULT 0 CHECK(dividend_reinvestment IN (0, 1)),
    updated_at TEXT NOT NULL
);

CREATE TABLE portfolio_rules (
    id INTEGER PRIMARY KEY NOT NULL CHECK(id = 1),
    bitcoin_target_percentage TEXT NOT NULL,
    bitcoin_caution_percentage TEXT NOT NULL,
    bitcoin_review_percentage TEXT NOT NULL,
    rebalance_threshold_percentage_points TEXT NOT NULL,
    price_stale_after_hours INTEGER NOT NULL CHECK(price_stale_after_hours BETWEEN 1 AND 8760),
    position_stale_after_hours INTEGER NOT NULL CHECK(position_stale_after_hours BETWEEN 1 AND 8760),
    updated_at TEXT NOT NULL
);

CREATE TABLE portfolio_valuation_snapshots (
    id TEXT PRIMARY KEY NOT NULL,
    as_of_date TEXT NOT NULL,
    as_of_utc TEXT NOT NULL,
    total_value TEXT NOT NULL,
    completeness_status TEXT NOT NULL CHECK(completeness_status IN ('complete', 'partial')),
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(as_of_date, source)
);

CREATE INDEX portfolio_valuations_date_idx
    ON portfolio_valuation_snapshots(as_of_date DESC);
"""

MIGRATION_4 = """
CREATE TABLE cash_roles (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    security_id TEXT NOT NULL REFERENCES securities(id),
    role TEXT NOT NULL CHECK(role IN
        ('emergency_reserve', 'spending_cash', 'investable_cash')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, security_id)
);

CREATE TABLE security_policy_mappings (
    security_id TEXT PRIMARY KEY NOT NULL REFERENCES securities(id),
    sleeve TEXT NOT NULL CHECK(sleeve IN
        ('us_equity', 'international_equity', 'nominal_treasury', 'tips',
         'investable_cash', 'bitcoin', 'unclassified')),
    eligible_for_taxable_purchase INTEGER NOT NULL DEFAULT 0
        CHECK(eligible_for_taxable_purchase IN (0, 1)),
    updated_at TEXT NOT NULL
);

CREATE TABLE household_policy_targets (
    sleeve TEXT PRIMARY KEY NOT NULL CHECK(sleeve IN
        ('us_equity', 'international_equity', 'nominal_treasury', 'tips',
         'investable_cash', 'bitcoin')),
    target_percentage TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE bitcoin_contribution_bands (
    id TEXT PRIMARY KEY NOT NULL,
    lower_bound_percentage TEXT NOT NULL,
    upper_bound_percentage TEXT NOT NULL,
    contribution_percentage TEXT NOT NULL,
    sort_order INTEGER NOT NULL UNIQUE CHECK(sort_order >= 0),
    updated_at TEXT NOT NULL
);

CREATE INDEX cash_roles_role_idx ON cash_roles(role);
CREATE INDEX security_policy_mappings_sleeve_idx ON security_policy_mappings(sleeve);
"""

MIGRATION_5 = """
ALTER TABLE position_snapshots ADD COLUMN superseded_by_batch_id TEXT
    REFERENCES import_batches(id);
ALTER TABLE position_snapshots ADD COLUMN manually_reversed_by_batch_id TEXT
    REFERENCES import_batches(id);

CREATE INDEX positions_superseded_by_batch_idx
    ON position_snapshots(superseded_by_batch_id);
"""

MIGRATION_6 = """
CREATE TABLE provider_connections (
    id TEXT PRIMARY KEY NOT NULL,
    provider_type TEXT NOT NULL CHECK(provider_type IN ('schwab')),
    status TEXT NOT NULL CHECK(status IN
        ('pending', 'connected', 'reconnect_required', 'disconnected')),
    key_version INTEGER NOT NULL,
    connected_at TEXT,
    last_successful_sync_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider_type)
);

CREATE TABLE provider_account_mappings (
    id TEXT PRIMARY KEY NOT NULL,
    connection_id TEXT NOT NULL REFERENCES provider_connections(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    encrypted_external_account_hash BLOB NOT NULL,
    external_account_hash_index TEXT NOT NULL UNIQUE,
    encrypted_last_four BLOB,
    status TEXT NOT NULL CHECK(status IN ('active', 'archived')),
    key_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(connection_id, account_id)
);

CREATE TABLE provider_sync_batches (
    id TEXT PRIMARY KEY NOT NULL,
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    mapping_id TEXT NOT NULL REFERENCES provider_account_mappings(id),
    status TEXT NOT NULL CHECK(status IN ('pending', 'committed', 'failed')),
    records_seen INTEGER NOT NULL DEFAULT 0 CHECK(records_seen >= 0),
    records_written INTEGER NOT NULL DEFAULT 0 CHECK(records_written >= 0),
    error_code TEXT,
    observed_at_utc TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(sync_run_id, mapping_id)
);

ALTER TABLE sync_runs ADD COLUMN connection_id TEXT REFERENCES provider_connections(id);
ALTER TABLE sync_runs ADD COLUMN trigger_type TEXT NOT NULL DEFAULT 'manual'
    CHECK(trigger_type IN ('manual', 'startup', 'scheduled'));
ALTER TABLE transactions ADD COLUMN provider_sync_batch_id TEXT REFERENCES provider_sync_batches(id);
ALTER TABLE position_snapshots ADD COLUMN provider_sync_batch_id TEXT REFERENCES provider_sync_batches(id);
ALTER TABLE market_price_snapshots ADD COLUMN provider_sync_batch_id TEXT
    REFERENCES provider_sync_batches(id);

CREATE INDEX provider_account_mappings_connection_idx
    ON provider_account_mappings(connection_id, status);
CREATE INDEX provider_sync_batches_run_idx ON provider_sync_batches(sync_run_id, status);
"""

MIGRATION_7 = """
CREATE TABLE provider_portfolio_snapshots (
    id TEXT PRIMARY KEY NOT NULL,
    connection_id TEXT NOT NULL REFERENCES provider_connections(id),
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    status TEXT NOT NULL CHECK(status IN ('published')),
    observed_at_utc TEXT NOT NULL,
    published_at TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK(provenance IN ('schwab_synthetic_read_only_v1')),
    reconciliation_status TEXT NOT NULL CHECK(reconciliation_status IN ('matched')),
    mapped_account_count INTEGER NOT NULL CHECK(mapped_account_count > 0),
    position_count INTEGER NOT NULL CHECK(position_count >= 0),
    audit_metadata_json TEXT NOT NULL,
    UNIQUE(sync_run_id)
);

CREATE TABLE provider_balance_snapshots (
    snapshot_id TEXT NOT NULL REFERENCES provider_portfolio_snapshots(id) ON DELETE CASCADE,
    mapping_id TEXT NOT NULL REFERENCES provider_account_mappings(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    liquidation_value TEXT NOT NULL,
    cash_balance TEXT NOT NULL,
    reconciliation_status TEXT NOT NULL CHECK(reconciliation_status IN ('matched')),
    PRIMARY KEY(snapshot_id, mapping_id)
);

CREATE TABLE provider_position_snapshots (
    snapshot_id TEXT NOT NULL REFERENCES provider_portfolio_snapshots(id) ON DELETE CASCADE,
    mapping_id TEXT NOT NULL REFERENCES provider_account_mappings(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    security_id TEXT NOT NULL REFERENCES securities(id),
    provider_instrument_key_index TEXT NOT NULL,
    quantity TEXT NOT NULL,
    market_value TEXT NOT NULL,
    average_price TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, mapping_id, provider_instrument_key_index)
);

CREATE INDEX provider_portfolio_snapshots_connection_idx
    ON provider_portfolio_snapshots(connection_id, published_at DESC);
CREATE INDEX provider_position_snapshots_security_idx
    ON provider_position_snapshots(snapshot_id, security_id);
"""

MIGRATION_8 = """
CREATE TABLE provider_instrument_bindings (
    connection_id TEXT NOT NULL REFERENCES provider_connections(id),
    provider_instrument_key_index TEXT NOT NULL,
    security_id TEXT NOT NULL REFERENCES securities(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(connection_id, provider_instrument_key_index)
);

CREATE INDEX provider_instrument_bindings_security_idx
    ON provider_instrument_bindings(security_id);
"""

MIGRATION_9 = """
DROP INDEX provider_position_snapshots_security_idx;
DROP INDEX provider_portfolio_snapshots_connection_idx;

ALTER TABLE provider_balance_snapshots RENAME TO provider_balance_snapshots_v8;
ALTER TABLE provider_position_snapshots RENAME TO provider_position_snapshots_v8;
ALTER TABLE provider_portfolio_snapshots RENAME TO provider_portfolio_snapshots_v8;

CREATE TABLE provider_portfolio_snapshots (
    id TEXT PRIMARY KEY NOT NULL,
    connection_id TEXT NOT NULL REFERENCES provider_connections(id),
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    status TEXT NOT NULL CHECK(status IN ('published')),
    observed_at_utc TEXT NOT NULL,
    published_at TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK(provenance IN
        ('schwab_synthetic_read_only_v1', 'schwab_http_read_only_v1')),
    reconciliation_status TEXT NOT NULL CHECK(reconciliation_status IN ('matched')),
    mapped_account_count INTEGER NOT NULL CHECK(mapped_account_count > 0),
    position_count INTEGER NOT NULL CHECK(position_count >= 0),
    audit_metadata_json TEXT NOT NULL,
    UNIQUE(sync_run_id)
);

INSERT INTO provider_portfolio_snapshots(
    id, connection_id, sync_run_id, status, observed_at_utc, published_at, provenance,
    reconciliation_status, mapped_account_count, position_count, audit_metadata_json
)
SELECT id, connection_id, sync_run_id, status, observed_at_utc, published_at, provenance,
       reconciliation_status, mapped_account_count, position_count, audit_metadata_json
FROM provider_portfolio_snapshots_v8;

CREATE TABLE provider_balance_snapshots (
    snapshot_id TEXT NOT NULL REFERENCES provider_portfolio_snapshots(id) ON DELETE CASCADE,
    mapping_id TEXT NOT NULL REFERENCES provider_account_mappings(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    liquidation_value TEXT NOT NULL,
    cash_balance TEXT NOT NULL,
    reconciliation_status TEXT NOT NULL CHECK(reconciliation_status IN ('matched')),
    PRIMARY KEY(snapshot_id, mapping_id)
);

INSERT INTO provider_balance_snapshots(
    snapshot_id, mapping_id, account_id, liquidation_value, cash_balance, reconciliation_status
)
SELECT snapshot_id, mapping_id, account_id, liquidation_value, cash_balance, reconciliation_status
FROM provider_balance_snapshots_v8;

CREATE TABLE provider_position_snapshots (
    snapshot_id TEXT NOT NULL REFERENCES provider_portfolio_snapshots(id) ON DELETE CASCADE,
    mapping_id TEXT NOT NULL REFERENCES provider_account_mappings(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    security_id TEXT NOT NULL REFERENCES securities(id),
    provider_instrument_key_index TEXT NOT NULL,
    quantity TEXT NOT NULL,
    market_value TEXT NOT NULL,
    average_price TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, mapping_id, provider_instrument_key_index)
);

INSERT INTO provider_position_snapshots(
    snapshot_id, mapping_id, account_id, security_id, provider_instrument_key_index,
    quantity, market_value, average_price
)
SELECT snapshot_id, mapping_id, account_id, security_id, provider_instrument_key_index,
       quantity, market_value, average_price
FROM provider_position_snapshots_v8;

DROP TABLE provider_balance_snapshots_v8;
DROP TABLE provider_position_snapshots_v8;
DROP TABLE provider_portfolio_snapshots_v8;

CREATE INDEX provider_portfolio_snapshots_connection_idx
    ON provider_portfolio_snapshots(connection_id, published_at DESC);
CREATE INDEX provider_position_snapshots_security_idx
    ON provider_position_snapshots(snapshot_id, security_id);
"""

MIGRATION_10 = """
ALTER TABLE transactions ADD COLUMN encrypted_provider_transaction_key BLOB;
ALTER TABLE transactions ADD COLUMN provider_transaction_key_index TEXT;
ALTER TABLE transactions ADD COLUMN provider_source_time_utc TEXT;

CREATE UNIQUE INDEX transactions_provider_key_unique
    ON transactions(account_id, provider_transaction_key_index)
    WHERE provider_transaction_key_index IS NOT NULL;

CREATE TABLE provider_transaction_checkpoints (
    id TEXT PRIMARY KEY NOT NULL,
    connection_id TEXT NOT NULL REFERENCES provider_connections(id),
    sync_run_id INTEGER NOT NULL UNIQUE REFERENCES sync_runs(id),
    status TEXT NOT NULL CHECK(status IN ('published')),
    window_start_date TEXT NOT NULL,
    window_end_date TEXT NOT NULL,
    imported_at_utc TEXT NOT NULL,
    published_at TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK(provenance IN
        ('schwab_synthetic_transactions_v1', 'schwab_http_transactions_v1')),
    reconciliation_status TEXT NOT NULL CHECK(reconciliation_status IN ('matched')),
    mapped_account_count INTEGER NOT NULL CHECK(mapped_account_count > 0),
    transaction_count INTEGER NOT NULL CHECK(transaction_count >= 0),
    audit_metadata_json TEXT NOT NULL
);

CREATE TABLE provider_transaction_checkpoint_records (
    checkpoint_id TEXT NOT NULL REFERENCES provider_transaction_checkpoints(id) ON DELETE CASCADE,
    mapping_id TEXT NOT NULL REFERENCES provider_account_mappings(id),
    transaction_id TEXT NOT NULL REFERENCES transactions(id),
    provider_transaction_key_index TEXT NOT NULL,
    source_record_hash TEXT NOT NULL,
    PRIMARY KEY(checkpoint_id, mapping_id, provider_transaction_key_index),
    UNIQUE(checkpoint_id, transaction_id)
);

CREATE INDEX provider_transaction_checkpoints_connection_idx
    ON provider_transaction_checkpoints(connection_id, sync_run_id DESC);
CREATE INDEX provider_transaction_checkpoint_records_transaction_idx
    ON provider_transaction_checkpoint_records(transaction_id);
"""

MIGRATION_11 = """
CREATE TABLE provider_price_checkpoints (
    id TEXT PRIMARY KEY NOT NULL,
    connection_id TEXT NOT NULL REFERENCES provider_connections(id),
    sync_run_id INTEGER NOT NULL UNIQUE REFERENCES sync_runs(id),
    status TEXT NOT NULL CHECK(status IN ('published')),
    published_at TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK(provenance IN
        ('schwab_synthetic_prices_v1', 'schwab_http_prices_v1')),
    completeness TEXT NOT NULL CHECK(completeness IN ('complete', 'incomplete')),
    price_stale_after_hours INTEGER NOT NULL CHECK(price_stale_after_hours BETWEEN 1 AND 8760),
    quote_source_status TEXT NOT NULL CHECK(quote_source_status IN
        ('succeeded', 'failed', 'not_required')),
    quote_source_error_code TEXT,
    bitcoin_source_status TEXT NOT NULL CHECK(bitcoin_source_status IN
        ('succeeded', 'failed', 'not_required')),
    bitcoin_source_error_code TEXT,
    held_security_count INTEGER NOT NULL CHECK(held_security_count > 0),
    observed_count INTEGER NOT NULL CHECK(observed_count >= 0),
    carried_forward_count INTEGER NOT NULL CHECK(carried_forward_count >= 0),
    manual_count INTEGER NOT NULL CHECK(manual_count >= 0),
    unknown_count INTEGER NOT NULL CHECK(unknown_count >= 0),
    audit_metadata_json TEXT NOT NULL,
    CHECK((quote_source_status = 'failed') = (quote_source_error_code IS NOT NULL)),
    CHECK((bitcoin_source_status = 'failed') = (bitcoin_source_error_code IS NOT NULL)),
    CHECK(observed_count + carried_forward_count + manual_count + unknown_count
          = held_security_count),
    CHECK((completeness = 'complete')
          = (carried_forward_count = 0 AND unknown_count = 0
             AND quote_source_status <> 'failed' AND bitcoin_source_status <> 'failed'))
);

CREATE TABLE provider_price_observations (
    checkpoint_id TEXT NOT NULL REFERENCES provider_price_checkpoints(id) ON DELETE CASCADE,
    security_id TEXT NOT NULL REFERENCES securities(id),
    source TEXT NOT NULL,
    price_status TEXT NOT NULL CHECK(price_status IN
        ('observed', 'carried_forward', 'manual', 'unknown')),
    price TEXT,
    previous_close TEXT,
    observed_at_utc TEXT,
    market_session TEXT CHECK(market_session IN ('normal', 'halted', 'closed')),
    quote_context TEXT,
    freshness TEXT NOT NULL CHECK(freshness IN ('fresh', 'stale', 'unknown')),
    PRIMARY KEY(checkpoint_id, security_id),
    -- An unusable observation is recorded as unknown with no value at all. The schema makes a
    -- silent zero unrepresentable rather than relying on the service alone.
    CHECK((price_status = 'unknown'
           AND price IS NULL AND observed_at_utc IS NULL AND freshness = 'unknown')
       OR (price_status <> 'unknown'
           AND price IS NOT NULL AND observed_at_utc IS NOT NULL AND freshness <> 'unknown'))
);

CREATE INDEX provider_price_checkpoints_connection_idx
    ON provider_price_checkpoints(connection_id, sync_run_id DESC);
CREATE INDEX provider_price_observations_security_idx
    ON provider_price_observations(security_id, checkpoint_id);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class DatabaseEncryptionError(RuntimeError):
    """Raised when SQLCipher is unavailable or a database is not encrypted."""


class Database:
    def __init__(self, path: Path, vault: Vault, encryption_key: bytes) -> None:
        if len(encryption_key) != 32:
            raise ValueError("Database encryption key must be exactly 32 bytes")
        self.path = path
        self.vault = vault
        self.encryption_key = encryption_key

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        if self.path.is_symlink():
            raise DatabaseEncryptionError("Refusing to open a symlink database")
        if not self.path.exists():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.path, flags, 0o600)
            os.close(descriptor)
        self.path.chmod(0o600)
        if self.path.exists() and self.path.stat().st_size > 0:
            with self.path.open("rb") as handle:
                header = handle.read(16)
            if header == b"SQLite format 3\x00":
                raise DatabaseEncryptionError(
                    "Refusing to open a plaintext SQLite database; migrate it before startup"
                )
        try:
            with closing(self.connect()) as conn:
                has_metadata = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_meta'"
                ).fetchone()
                if has_metadata is None:
                    conn.executescript(SCHEMA_V1)
                    conn.execute(
                        "INSERT INTO app_meta(key, value, updated_at) VALUES('schema_version', '1', ?)",
                        (utc_now(),),
                    )
                    conn.commit()
                row = conn.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()
                if row is None:
                    raise DatabaseEncryptionError("Encrypted database has no schema version")
                version = int(row[0])
                if version > LATEST_SCHEMA_VERSION:
                    raise DatabaseEncryptionError("Encrypted database schema is newer than this build")
                migrations = {
                    1: MIGRATION_2,
                    2: MIGRATION_3,
                    3: MIGRATION_4,
                    4: MIGRATION_5,
                    5: MIGRATION_6,
                    6: MIGRATION_7,
                    7: MIGRATION_8,
                    8: MIGRATION_9,
                    9: MIGRATION_10,
                    10: MIGRATION_11,
                }
                while version < LATEST_SCHEMA_VERSION:
                    migration = migrations.get(version)
                    if migration is None:
                        raise DatabaseEncryptionError("No migration path exists for encrypted database")
                    next_version = version + 1
                    migrated_at = utc_now().replace("'", "''")
                    conn.executescript(
                        f"BEGIN IMMEDIATE;\n{migration}\n"
                        f"UPDATE app_meta SET value='{next_version}', updated_at='{migrated_at}' "
                        "WHERE key='schema_version';\nCOMMIT;"
                    )
                    version = next_version
        finally:
            self.path.chmod(0o600)

    def connect(self, *, journal_mode: str = "WAL") -> sqlite3.Connection:
        if journal_mode not in {"WAL", "DELETE"}:
            raise ValueError("Unsupported SQLite journal mode")
        conn = sqlite3.connect(str(self.path), timeout=10)
        try:
            conn.execute(f"PRAGMA key = \"x'{self.encryption_key.hex()}'\"")
            conn.execute("PRAGMA cipher_compatibility = 4")
            cipher_version = conn.execute("PRAGMA cipher_version").fetchone()
            if not cipher_version or not cipher_version[0]:
                raise DatabaseEncryptionError("SQLCipher support is unavailable")
            conn.execute("PRAGMA cipher_memory_security = ON")
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA journal_mode = {journal_mode}")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA trusted_schema = OFF")
        except Exception:
            conn.close()
            raise
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(self.connect()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def upsert_secure_record(self, category: str, external_id: str, payload: dict[str, Any]) -> None:
        category = category.strip().lower()
        lookup_hash = self.vault.blind_index(category, external_id)
        aad = f"secure-record:{category}:{lookup_hash}".encode()
        encrypted = self.vault.encrypt_json(payload, associated_data=aad)
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO secure_records
                    (category, lookup_hash, encrypted_payload, key_version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(category, lookup_hash) DO UPDATE SET
                    encrypted_payload=excluded.encrypted_payload,
                    key_version=excluded.key_version,
                    updated_at=excluded.updated_at
                """,
                (category, lookup_hash, encrypted, self.vault.key_version, now, now),
            )

    def get_secure_record(self, category: str, external_id: str) -> dict[str, Any] | None:
        category = category.strip().lower()
        lookup_hash = self.vault.blind_index(category, external_id)
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT encrypted_payload FROM secure_records WHERE category=? AND lookup_hash=?",
                (category, lookup_hash),
            ).fetchone()
        if row is None:
            return None
        aad = f"secure-record:{category}:{lookup_hash}".encode()
        return self.vault.decrypt_json(row["encrypted_payload"], associated_data=aad)

    def add_audit_event(
        self,
        event_type: str,
        actor: str,
        *,
        subject: str | None = None,
        metadata: dict[str, str | int | bool] | None = None,
    ) -> None:
        actor_hash = self.vault.blind_index("actor", actor)
        subject_hash = self.vault.blind_index("subject", subject) if subject else None
        safe_metadata = json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO audit_events(event_type, actor_hash, subject_hash, metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (event_type, actor_hash, subject_hash, safe_metadata, utc_now()),
            )

    def health(self) -> bool:
        if not self.path.exists():
            return False
        try:
            with closing(self.connect()) as conn:
                row = conn.execute("PRAGMA quick_check").fetchone()
                cipher_errors = conn.execute("PRAGMA cipher_integrity_check").fetchall()
        except sqlite3.DatabaseError:
            return False
        return bool(row and row[0] == "ok" and not cipher_errors)

    def schema_version(self) -> int:
        """Read the encrypted database schema without migrating or changing journal state."""
        if not self.path.exists() or self.path.is_symlink():
            raise DatabaseEncryptionError("Encrypted database is unavailable")
        connection = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        try:
            connection.execute(f"PRAGMA key = \"x'{self.encryption_key.hex()}'\"")
            connection.execute("PRAGMA cipher_compatibility = 4")
            cipher_version = connection.execute("PRAGMA cipher_version").fetchone()
            if not cipher_version or not cipher_version[0]:
                raise DatabaseEncryptionError("SQLCipher support is unavailable")
            connection.execute("PRAGMA cipher_memory_security = ON")
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()
        except sqlite3.DatabaseError as exc:
            raise DatabaseEncryptionError("Encrypted database schema is unavailable") from exc
        finally:
            connection.close()
        if row is None:
            raise DatabaseEncryptionError("Encrypted database has no schema version")
        version = int(row[0])
        if version < 1 or version > LATEST_SCHEMA_VERSION:
            raise DatabaseEncryptionError("Encrypted database schema is incompatible")
        return version

    def verify_critical_invariants(self, expected_schema_version: int = LATEST_SCHEMA_VERSION) -> int:
        """Validate schema compatibility and relational integrity without migration."""
        if not 1 <= expected_schema_version <= LATEST_SCHEMA_VERSION:
            raise ValueError("Expected schema version is unsupported")
        with closing(self.connect(journal_mode="DELETE")) as conn:
            row = conn.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()
            if row is None or int(row[0]) != expected_schema_version:
                raise DatabaseEncryptionError("Restored database schema is incompatible")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise DatabaseEncryptionError("Restored database failed relational integrity")
        return expected_schema_version

    def backup_to(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.parent.chmod(0o700)
        with closing(self.connect()) as source:
            source.execute("PRAGMA wal_checkpoint(FULL)")
            snapshot = Database(destination, self.vault, self.encryption_key)
            with closing(snapshot.connect(journal_mode="DELETE")) as target:
                source.backup(target)
                target.commit()
                row = target.execute("PRAGMA quick_check").fetchone()
                cipher_errors = target.execute("PRAGMA cipher_integrity_check").fetchall()
                if not row or row[0] != "ok" or cipher_errors:
                    raise DatabaseEncryptionError("SQLCipher snapshot failed integrity validation")
        destination.chmod(0o600)

    def at_path(self, path: Path) -> Database:
        return Database(path, self.vault, self.encryption_key)

    def verify_encrypted_records(self) -> int:
        with closing(self.connect(journal_mode="DELETE")) as conn:
            rows = conn.execute(
                "SELECT category, lookup_hash, encrypted_payload FROM secure_records"
            ).fetchall()
            portfolio_fields = [
                ("institutions", "encrypted_name", "name"),
                ("providers", "encrypted_name", "name"),
                ("providers", "encrypted_identifier", "identifier"),
                ("accounts", "encrypted_name", "name"),
                ("accounts", "encrypted_provider_identifier", "provider_identifier"),
                ("accounts", "encrypted_notes", "notes"),
                ("securities", "encrypted_symbol", "symbol"),
                ("securities", "encrypted_name", "name"),
                ("transactions", "encrypted_description", "description"),
                ("transactions", "encrypted_notes", "notes"),
                ("account_targets", "encrypted_notes", "notes"),
                ("cash_envelopes", "encrypted_name", "name"),
                ("cash_envelopes", "encrypted_notes", "notes"),
                ("import_batches", "encrypted_summary", "summary"),
                ("import_previews", "encrypted_payload", "payload"),
            ]
            transaction_columns = {
                str(row["name"]) for row in conn.execute("PRAGMA table_info(transactions)")
            }
            if "encrypted_provider_transaction_key" in transaction_columns:
                portfolio_fields.append(
                    (
                        "transactions",
                        "encrypted_provider_transaction_key",
                        "provider_transaction_key",
                    )
                )
            encrypted_rows: list[tuple[str, str, str, bytes]] = []
            for table, column, field in portfolio_fields:
                for record in conn.execute(
                    f"SELECT id, {column} AS envelope FROM {table} "  # noqa: S608
                    f"WHERE {column} IS NOT NULL"  # noqa: S608
                    if table != "import_previews"
                    else "SELECT batch_id AS id, encrypted_payload AS envelope "
                    "FROM import_previews WHERE encrypted_payload IS NOT NULL"
                ).fetchall():
                    encrypted_rows.append((table, record["id"], field, record["envelope"]))
        for row in rows:
            aad = f"secure-record:{row['category']}:{row['lookup_hash']}".encode()
            self.vault.decrypt_json(row["encrypted_payload"], associated_data=aad)
        for table, record_id, field, envelope in encrypted_rows:
            aad = f"portfolio-field:{table}:{record_id}:{field}".encode()
            self.vault.decrypt_json(envelope, associated_data=aad)
        return len(rows) + len(encrypted_rows)
