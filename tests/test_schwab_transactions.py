from __future__ import annotations

import asyncio
import uuid
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from northstar.config import Settings
from northstar.crypto import CryptoError
from northstar.data import Database, PortfolioRepository, ProviderConnectionRepository
from northstar.data import database as database_module
from northstar.data.portfolio import decrypted_json, encrypted_json
from northstar.integrations import SchwabTokenGrant, SchwabTransaction, SchwabTransactionItem
from northstar.services import BackupService, SchwabTransactionService, TransactionRefreshError


def connected_repository(
    settings: Settings,
) -> tuple[Database, PortfolioRepository, ProviderConnectionRepository, str]:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    database.initialize()
    portfolio = PortfolioRepository(database)
    connections = ProviderConnectionRepository(database)
    connection, state = connections.start_oauth("synthetic-transaction-session")
    attempt = connections.consume_oauth(state, "synthetic-transaction-session")
    now = datetime.now(UTC)
    synthetic_access = "-".join(("synthetic", "transaction", "access", "credential"))
    synthetic_refresh = "-".join(("synthetic", "transaction", "refresh", "credential"))
    bearer_type = "Bearer"
    connections.save_token_grant(
        attempt,
        SchwabTokenGrant(
            access_token=synthetic_access,
            refresh_token=synthetic_refresh,
            token_type=bearer_type,
            expires_at=now + timedelta(minutes=30),
            refresh_expires_at=now + timedelta(days=7),
            scopes=("synthetic-read",),
        ),
    )
    return database, portfolio, connections, connection.id


def add_security(database: Database, *, symbol: str = "SYNX") -> str:
    security_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    with database.transaction() as conn:
        conn.execute(
            """INSERT INTO securities(
                   id, instrument_type, currency, encrypted_symbol, symbol_hash,
                   encrypted_name, key_version, created_at, updated_at, asset_class
               ) VALUES (?, 'etf', 'USD', ?, ?, ?, ?, ?, ?, 'equity')""",
            (
                security_id,
                encrypted_json(database, "securities", security_id, "symbol", symbol),
                database.vault.blind_index("security-symbol", symbol),
                encrypted_json(database, "securities", security_id, "name", "Synthetic ETF"),
                database.vault.key_version,
                now,
                now,
            ),
        )
    return security_id


def security_item(
    *,
    effect: str | None = None,
    amount: str | None = None,
    price: str | None = None,
) -> SchwabTransactionItem:
    return SchwabTransactionItem(
        provider_instrument_key="synthetic-transaction-instrument-key",
        symbol="SYNX",
        instrument_type="equity",
        amount=Decimal(amount) if amount is not None else None,
        cost=None,
        price=Decimal(price) if price is not None else None,
        fee_type=None,
        position_effect=effect,
    )


def fee_item(amount: str = "1") -> SchwabTransactionItem:
    return SchwabTransactionItem(
        provider_instrument_key=None,
        symbol=None,
        instrument_type=None,
        amount=Decimal(amount),
        cost=None,
        price=None,
        fee_type="COMMISSION",
        position_effect=None,
    )


def transaction(
    provider_hash: str,
    key: int,
    provider_type: str,
    net_amount: str,
    *,
    items: tuple[SchwabTransactionItem, ...] = (),
    activity_type: str = "EXECUTION",
    status: str = "VALID",
    imported_at: datetime | None = None,
) -> SchwabTransaction:
    return SchwabTransaction(
        provider_transaction_key=str(key),
        provider_account_hash=provider_hash,
        provider_source_time_utc=datetime(2026, 8, 15, 14, 30, tzinfo=UTC),
        trade_date=datetime(2026, 8, 14, 12, tzinfo=UTC),
        settlement_date=datetime(2026, 8, 17, 12, tzinfo=UTC),
        imported_at_utc=imported_at or datetime(2026, 8, 16, 12, tzinfo=UTC),
        provider_type=provider_type,
        provider_status=status,
        activity_type=activity_type,
        sub_account="CASH",
        net_amount=Decimal(net_amount),
        items=items,
    )


def test_transaction_checkpoint_normalizes_supported_semantics_atomically(
    settings: Settings,
) -> None:
    database, portfolio, connections, connection_id = connected_repository(settings)
    account = portfolio.create_account(
        name="Synthetic transaction account",
        account_type="roth_ira",
        tax_treatment="tax_free",
    )
    provider_hash = "synthetic-transaction-provider-hash"
    connections.map_account(connection_id, account.id, external_account_hash=provider_hash)
    security_id = add_security(database)
    records = (
        transaction(provider_hash, 101, "ACH_RECEIPT", "25", activity_type="TRANSFER"),
        transaction(provider_hash, 102, "CASH_DISBURSEMENT", "-5", activity_type="TRANSFER"),
        transaction(provider_hash, 103, "JOURNAL", "7", activity_type="TRANSFER"),
        transaction(provider_hash, 104, "ELECTRONIC_FUND", "-3", activity_type="TRANSFER"),
        transaction(
            provider_hash,
            105,
            "TRADE",
            "-101",
            items=(security_item(effect="OPENING", amount="2", price="50"), fee_item()),
        ),
        transaction(
            provider_hash,
            106,
            "TRADE",
            "59",
            items=(security_item(effect="CLOSING", amount="1", price="60"), fee_item()),
        ),
        transaction(
            provider_hash,
            107,
            "TRADE",
            "0",
            items=(security_item(effect="AUTOMATIC", amount="0.5", price="10"),),
        ),
        transaction(
            provider_hash,
            108,
            "DIVIDEND_OR_INTEREST",
            "4",
            items=(security_item(),),
        ),
        transaction(provider_hash, 109, "DIVIDEND_OR_INTEREST", "2"),
        transaction(provider_hash, 110, "MEMORANDUM", "-1", items=(fee_item(),)),
    )

    async def load(candidate: str, start: date, end: date) -> tuple[SchwabTransaction, ...]:
        assert candidate == provider_hash
        assert (start, end) == (date(2026, 8, 1), date(2026, 8, 16))
        return records

    service = SchwabTransactionService(database, connections)
    publication = asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor="synthetic@example.com",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 16),
            load_transactions=load,
        )
    )

    assert publication.transaction_count == 10
    assert publication.mapped_account_count == 1
    assert publication.reconciliation_status == "matched"
    assert service.current(connection_id) == publication
    with closing(database.connect()) as conn:
        rows = conn.execute(
            """SELECT transaction_type, security_id, trade_date, settlement_date,
                      imported_at, provider_source_time_utc, quantity, price, fees,
                      fees_status, net_amount, net_amount_status, cost_basis_status,
                      encrypted_provider_transaction_key, provider_transaction_key_index,
                      source_record_hash
               FROM transactions ORDER BY provider_transaction_key_index"""
        ).fetchall()
        checkpoint_count = conn.execute(
            "SELECT COUNT(*) FROM provider_transaction_checkpoints"
        ).fetchone()[0]
        link_count = conn.execute(
            "SELECT COUNT(*) FROM provider_transaction_checkpoint_records"
        ).fetchone()[0]
        audit = conn.execute(
            """SELECT audit_metadata_json FROM provider_transaction_checkpoints
               WHERE id=?""",
            (publication.id,),
        ).fetchone()[0]
    assert {row["transaction_type"] for row in rows} == {
        "buy",
        "sell",
        "dividend",
        "reinvestment",
        "interest",
        "deposit",
        "withdrawal",
        "transfer_in",
        "transfer_out",
        "fee",
    }
    assert checkpoint_count == 1
    assert link_count == 10
    assert all(row["net_amount_status"] == "supplied" for row in rows)
    assert all(row["provider_source_time_utc"].endswith("+00:00") for row in rows)
    assert all(row["imported_at"].endswith("+00:00") for row in rows)
    assert all(row["trade_date"] == "2026-08-14" for row in rows)
    assert all(row["settlement_date"] == "2026-08-17" for row in rows)
    assert all(row["encrypted_provider_transaction_key"].startswith(b"NSV1") for row in rows)
    assert all(b"synthetic" not in row["encrypted_provider_transaction_key"] for row in rows)
    provider_keys = {str(key) for key in range(101, 111)}
    assert all(row["provider_transaction_key_index"] not in provider_keys for row in rows)
    assert all(row["source_record_hash"] not in {str(key) for key in range(101, 111)} for row in rows)
    trade_rows = [row for row in rows if row["transaction_type"] in {"buy", "sell"}]
    assert all(row["security_id"] == security_id for row in trade_rows)
    assert all(row["quantity"] is not None and row["price"] is not None for row in trade_rows)
    assert all(row["fees"] == "1.00" and row["fees_status"] == "supplied" for row in trade_rows)
    assert all(row["cost_basis_status"] == "unknown" for row in trade_rows)
    assert provider_hash not in audit
    assert "synthetic-transaction-instrument-key" not in audit
    assert "raw_response_stored" in audit


def test_transaction_replay_and_changed_provider_identity_preserve_checkpoint(
    settings: Settings,
) -> None:
    database, portfolio, connections, connection_id = connected_repository(settings)
    account = portfolio.create_account(
        name="Synthetic replay account", account_type="hsa", tax_treatment="tax_advantaged"
    )
    provider_hash = "synthetic-replay-provider-hash"
    connections.map_account(connection_id, account.id, external_account_hash=provider_hash)
    first_record = transaction(provider_hash, 201, "ACH_RECEIPT", "10", activity_type="TRANSFER")
    service = SchwabTransactionService(database, connections)

    async def first_load(_: str, __: date, ___: date) -> tuple[SchwabTransaction, ...]:
        return (first_record,)

    first = asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor="synthetic@example.com",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 16),
            load_transactions=first_load,
        )
    )

    async def replay(_: str, __: date, ___: date) -> tuple[SchwabTransaction, ...]:
        return (
            transaction(
                provider_hash,
                201,
                "ACH_RECEIPT",
                "10",
                activity_type="TRANSFER",
                imported_at=datetime(2026, 8, 16, 13, tzinfo=UTC),
            ),
        )

    with pytest.raises(TransactionRefreshError, match="provider_transaction_replay"):
        asyncio.run(
            service.refresh(
                connection_id=connection_id,
                actor="synthetic@example.com",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 16),
                load_transactions=replay,
            )
        )

    async def changed(_: str, __: date, ___: date) -> tuple[SchwabTransaction, ...]:
        return (transaction(provider_hash, 201, "ACH_RECEIPT", "11", activity_type="TRANSFER"),)

    with pytest.raises(TransactionRefreshError, match="provider_transaction_changed"):
        asyncio.run(
            service.refresh(
                connection_id=connection_id,
                actor="synthetic@example.com",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 16),
                load_transactions=changed,
            )
        )
    assert service.current(connection_id) == first
    with closing(database.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM provider_transaction_checkpoints").fetchone()[0] == 1
        failed = conn.execute(
            """SELECT error_code FROM sync_runs WHERE status='failed' ORDER BY id"""
        ).fetchall()
    assert [row[0] for row in failed] == [
        "provider_transaction_replay",
        "provider_transaction_changed",
    ]


def test_transaction_replay_uses_matching_historical_window(settings: Settings) -> None:
    database, portfolio, connections, connection_id = connected_repository(settings)
    account = portfolio.create_account(
        name="Synthetic historical replay account",
        account_type="hsa",
        tax_treatment="tax_advantaged",
    )
    provider_hash = "synthetic-historical-replay-provider-hash"
    connections.map_account(connection_id, account.id, external_account_hash=provider_hash)
    service = SchwabTransactionService(database, connections)
    first_record = transaction(provider_hash, 211, "ACH_RECEIPT", "10", activity_type="TRANSFER")
    second_record = transaction(provider_hash, 212, "ACH_RECEIPT", "20", activity_type="TRANSFER")

    async def load_first(_: str, __: date, ___: date) -> tuple[SchwabTransaction, ...]:
        return (first_record,)

    async def load_second(_: str, __: date, ___: date) -> tuple[SchwabTransaction, ...]:
        return (second_record,)

    asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor="synthetic@example.com",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 16),
            load_transactions=load_first,
        )
    )
    second = asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor="synthetic@example.com",
            start_date=date(2026, 8, 15),
            end_date=date(2026, 8, 16),
            load_transactions=load_second,
        )
    )

    with pytest.raises(TransactionRefreshError, match="provider_transaction_replay"):
        asyncio.run(
            service.refresh(
                connection_id=connection_id,
                actor="synthetic@example.com",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 16),
                load_transactions=load_first,
            )
        )

    assert service.current(connection_id) == second
    with closing(database.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM provider_transaction_checkpoints").fetchone()[0] == 2


def test_transaction_replay_rejects_records_removed_from_matching_window(
    settings: Settings,
) -> None:
    database, portfolio, connections, connection_id = connected_repository(settings)
    account = portfolio.create_account(
        name="Synthetic removal replay account",
        account_type="hsa",
        tax_treatment="tax_advantaged",
    )
    provider_hash = "synthetic-removal-replay-provider-hash"
    connections.map_account(connection_id, account.id, external_account_hash=provider_hash)
    service = SchwabTransactionService(database, connections)

    async def load_nonempty(_: str, __: date, ___: date) -> tuple[SchwabTransaction, ...]:
        return (
            transaction(provider_hash, 221, "ACH_RECEIPT", "10", activity_type="TRANSFER"),
        )

    async def load_empty(_: str, __: date, ___: date) -> tuple[SchwabTransaction, ...]:
        return ()

    first = asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor="synthetic@example.com",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 16),
            load_transactions=load_nonempty,
        )
    )

    with pytest.raises(TransactionRefreshError, match="provider_transaction_changed"):
        asyncio.run(
            service.refresh(
                connection_id=connection_id,
                actor="synthetic@example.com",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 16),
                load_transactions=load_empty,
            )
        )

    assert service.current(connection_id) == first
    with closing(database.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM provider_transaction_checkpoints").fetchone()[0] == 1


def test_encrypted_transaction_identity_is_authenticated_during_backup_recovery(
    settings: Settings,
    tmp_path: Path,
) -> None:
    database, portfolio, connections, connection_id = connected_repository(settings)
    account = portfolio.create_account(
        name="Synthetic recovery account",
        account_type="hsa",
        tax_treatment="tax_advantaged",
    )
    provider_hash = "synthetic-recovery-provider-hash"
    connections.map_account(connection_id, account.id, external_account_hash=provider_hash)
    service = SchwabTransactionService(database, connections)

    async def load(_: str, __: date, ___: date) -> tuple[SchwabTransaction, ...]:
        return (transaction(provider_hash, 231, "ACH_RECEIPT", "10", activity_type="TRANSFER"),)

    asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor="synthetic@example.com",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 16),
            load_transactions=load,
        )
    )
    backups = BackupService(database, settings.backup_dir, settings.backup_vault())
    backup = backups.create(prune=False)
    restored_path = tmp_path / "restored-transactions" / "northstar.db"
    backups.restore_to(backup, restored_path)
    restored = database.at_path(restored_path)
    with closing(restored.connect()) as conn:
        row = conn.execute(
            """SELECT id, encrypted_provider_transaction_key
               FROM transactions WHERE provider_transaction_key_index IS NOT NULL"""
        ).fetchone()
    assert decrypted_json(
        restored,
        "transactions",
        str(row["id"]),
        "provider_transaction_key",
        row["encrypted_provider_transaction_key"],
    ) == "231"

    with database.transaction() as conn:
        source = conn.execute(
            """SELECT id, encrypted_provider_transaction_key
               FROM transactions WHERE provider_transaction_key_index IS NOT NULL"""
        ).fetchone()
        corrupted = bytearray(source["encrypted_provider_transaction_key"])
        corrupted[-1] ^= 1
        conn.execute(
            "UPDATE transactions SET encrypted_provider_transaction_key=? WHERE id=?",
            (bytes(corrupted), source["id"]),
        )
    existing_backups = set(settings.backup_dir.glob("*.nsbak"))
    with pytest.raises(CryptoError, match="authentication"):
        backups.create(prune=False)
    assert set(settings.backup_dir.glob("*.nsbak")) == existing_backups


def test_transaction_partial_or_unsupported_history_writes_nothing(
    settings: Settings,
) -> None:
    database, portfolio, connections, connection_id = connected_repository(settings)
    first = portfolio.create_account(
        name="First synthetic account", account_type="roth_ira", tax_treatment="tax_free"
    )
    second = portfolio.create_account(
        name="Second synthetic account", account_type="hsa", tax_treatment="tax_advantaged"
    )
    first_hash = "synthetic-partial-first-hash"
    second_hash = "synthetic-partial-second-hash"
    connections.map_accounts(
        connection_id,
        ((first_hash, first.id, None), (second_hash, second.id, None)),
    )
    service = SchwabTransactionService(database, connections)

    async def load(candidate: str, _: date, __: date) -> tuple[SchwabTransaction, ...]:
        if candidate == first_hash:
            return (transaction(first_hash, 301, "ACH_RECEIPT", "10", activity_type="TRANSFER"),)
        return (transaction(second_hash, 302, "MONEY_MARKET", "2"),)

    with pytest.raises(TransactionRefreshError, match="provider_transaction_semantics_unsupported"):
        asyncio.run(
            service.refresh(
                connection_id=connection_id,
                actor="synthetic@example.com",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 16),
                load_transactions=load,
            )
        )
    assert service.current(connection_id) is None
    with closing(database.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM provider_transaction_checkpoints").fetchone()[0] == 0
        run = conn.execute(
            "SELECT status, records_written, error_code FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert tuple(run) == ("failed", 0, "provider_transaction_semantics_unsupported")


def test_transaction_refresh_cancellation_preserves_prior_state(settings: Settings) -> None:
    database, portfolio, connections, connection_id = connected_repository(settings)
    account = portfolio.create_account(
        name="Synthetic cancellation account",
        account_type="taxable_brokerage",
        tax_treatment="taxable",
    )
    provider_hash = "synthetic-cancel-transaction-hash"
    connections.map_account(connection_id, account.id, external_account_hash=provider_hash)
    service = SchwabTransactionService(database, connections)

    async def exercise() -> None:
        started = asyncio.Event()

        async def blocked(_: str, __: date, ___: date) -> tuple[SchwabTransaction, ...]:
            started.set()
            await asyncio.Event().wait()
            return ()

        task = asyncio.create_task(
            service.refresh(
                connection_id=connection_id,
                actor="synthetic@example.com",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 16),
                load_transactions=blocked,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    with closing(database.connect()) as conn:
        run = conn.execute(
            "SELECT status, error_code FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM provider_transaction_checkpoints").fetchone()[0] == 0
    assert tuple(run) == ("failed", "transaction_refresh_cancelled")


def test_transaction_checkpoint_migration_preserves_existing_manual_transactions(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(database_module, "LATEST_SCHEMA_VERSION", 9)
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    database.initialize()
    portfolio = PortfolioRepository(database)
    account = portfolio.create_account(
        name="Synthetic pre-migration account",
        account_type="cash",
        tax_treatment="not_applicable",
    )
    transaction_id = str(uuid.uuid4())
    with database.transaction() as conn:
        conn.execute(
            """INSERT INTO transactions(
                   id, account_id, transaction_type, trade_date, imported_at,
                   gross_amount_status, net_amount, net_amount_status,
                   cost_basis_status, fees_status, source_record_hash, key_version
               ) VALUES (?, ?, 'deposit', '2026-08-01', ?, 'unknown', '10.00',
                         'supplied', 'not_applicable', 'not_applicable', ?, ?)""",
            (
                transaction_id,
                account.id,
                datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
                database.vault.blind_index("synthetic-pre-migration", transaction_id),
                database.vault.key_version,
            ),
        )

    monkeypatch.setattr(database_module, "LATEST_SCHEMA_VERSION", 10)
    database.initialize()

    with closing(database.connect()) as conn:
        row = conn.execute(
            """SELECT transaction_type, net_amount, encrypted_provider_transaction_key,
                      provider_transaction_key_index, provider_source_time_utc
               FROM transactions WHERE id=?""",
            (transaction_id,),
        ).fetchone()
        checkpoint_table = conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='provider_transaction_checkpoints'"""
        ).fetchone()
    assert tuple(row) == ("deposit", "10.00", None, None, None)
    assert checkpoint_table is not None
