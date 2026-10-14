from __future__ import annotations

import logging
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from northstar.config import Settings
from northstar.data import Database, PortfolioRepository
from northstar.imports import MAX_FILE_BYTES, MAX_ROWS, ImportValidationError, adapter_for
from northstar.services import BackupService, PortfolioImportService

ACCOUNT_IDENTIFIER = "SYNTHETIC-ACCT-4242"
TRANSACTION_HEADER = (
    "Account Number,Trade Date,Settlement Date,Action,Symbol,Description,Quantity,Price,"
    "Fees,Amount,Cost Basis\n"
)
POSITION_HEADER = (
    "Row Type,Account Number,Symbol,Description,Security Type,Quantity,Price,Market Value,"
    "Cost Basis,As Of Date\n"
)


def import_stack(settings: Settings) -> tuple[Database, PortfolioRepository, PortfolioImportService, str]:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    database.initialize()
    repository = PortfolioRepository(database)
    account = repository.create_account(
        name="Invented Roth",
        account_type="roth_ira",
        tax_treatment="tax_free",
        provider_identifier=ACCOUNT_IDENTIFIER,
    )
    return database, repository, PortfolioImportService(database, repository), account.id


def transaction_csv(rows: list[str]) -> bytes:
    return (TRANSACTION_HEADER + "\n".join(rows) + "\n").encode()


def synthetic_transaction_rows() -> list[str]:
    return [
        f"{ACCOUNT_IDENTIFIER},2026-01-02,2026-01-05,BUY,SYNX,Invented buy,2,10.005,0.01,-20.02,20.01",
        f"{ACCOUNT_IDENTIFIER},2026-01-06,2026-01-07,SELL,SYNX,Invented sell,1,11.00,0.02,10.98,10.00",
        f"{ACCOUNT_IDENTIFIER},2026-01-08,,DIVIDEND,SYNX,Invented dividend,,,,1.25,",
        f"{ACCOUNT_IDENTIFIER},2026-01-09,2026-01-12,REINVESTMENT,SYNX,Invented reinvest,0.1,12.00,,,1.20",
        f"{ACCOUNT_IDENTIFIER},2026-01-10,,INTEREST,,Invented interest,,,,0.33,",
        f"{ACCOUNT_IDENTIFIER},2026-01-11,,DEPOSIT,,Invented deposit,,,,100.00,",
        f"{ACCOUNT_IDENTIFIER},2026-01-12,,WITHDRAWAL,,Invented withdrawal,,,,-5.00,",
        f"{ACCOUNT_IDENTIFIER},2026-01-13,,TRANSFER IN,,Invented transfer in,,,,25.00,",
        f"{ACCOUNT_IDENTIFIER},2026-01-14,,TRANSFER OUT,,Invented transfer out,,,,-10.00,",
        f"{ACCOUNT_IDENTIFIER},2026-01-15,,FEE,,Invented fee,,,,-0.50,",
        f"{ACCOUNT_IDENTIFIER},2026-01-16,,ADJUSTMENT,,Invented adjustment,,,,0.01,",
    ]


def test_every_transaction_type_commits_with_explicit_unknown_states(settings: Settings) -> None:
    database, _, service, account_id = import_stack(settings)
    preview = service.preview(
        "schwab_transactions_synthetic_v1",
        account_id,
        transaction_csv(synthetic_transaction_rows()),
    )

    committed = service.commit(preview.batch.id, "synthetic-actor")
    with closing(database.connect()) as conn:
        rows = conn.execute(
            "SELECT transaction_type, fees_status, cost_basis_status, imported_at, "
            "trade_date, settlement_date FROM transactions ORDER BY trade_date"
        ).fetchall()
        audit_metadata = " ".join(
            row[0] for row in conn.execute("SELECT metadata_json FROM audit_events").fetchall()
        )

    assert committed.status == "committed"
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
        "adjustment",
    }
    assert all(row["imported_at"].endswith("+00:00") for row in rows)
    assert rows[0]["trade_date"] == "2026-01-02"
    assert rows[0]["settlement_date"] == "2026-01-05"
    assert any(row["fees_status"] == "unknown" for row in rows)
    assert any(row["cost_basis_status"] == "not_applicable" for row in rows)
    assert ACCOUNT_IDENTIFIER not in audit_metadata
    assert "20.02" not in audit_metadata


def test_duplicate_import_is_idempotent(settings: Settings) -> None:
    database, _, service, account_id = import_stack(settings)
    payload = transaction_csv([synthetic_transaction_rows()[5]])
    first = service.preview("schwab_transactions_synthetic_v1", account_id, payload)
    service.commit(first.batch.id, "synthetic-actor")

    duplicate = service.preview("schwab_transactions_synthetic_v1", account_id, payload)
    with closing(database.connect()) as conn:
        count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    assert duplicate.duplicate
    assert duplicate.batch.id == first.batch.id
    assert duplicate.batch.status == "committed"
    assert count == 1


def test_commit_failure_rolls_back_all_records(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    database, _, service, account_id = import_stack(settings)
    preview = service.preview(
        "schwab_transactions_synthetic_v1",
        account_id,
        transaction_csv(synthetic_transaction_rows()[:2]),
    )
    original = service._insert_record
    calls = 0

    def fail_second(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic commit failure")
        original(*args, **kwargs)

    monkeypatch.setattr(service, "_insert_record", fail_second)
    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        service.commit(preview.batch.id, "synthetic-actor")

    with closing(database.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM securities").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tax_lots").fetchone()[0] == 0
        status = conn.execute("SELECT status FROM import_batches WHERE id=?", (preview.batch.id,)).fetchone()[
            0
        ]
    assert status == "preview"


def test_reversal_only_marks_records_owned_by_selected_batch(settings: Settings) -> None:
    database, _, service, account_id = import_stack(settings)
    first = service.preview(
        "schwab_transactions_synthetic_v1",
        account_id,
        transaction_csv([synthetic_transaction_rows()[5]]),
    )
    service.commit(first.batch.id, "synthetic-actor")
    second = service.preview(
        "schwab_transactions_synthetic_v1",
        account_id,
        transaction_csv([synthetic_transaction_rows()[6]]),
    )
    service.commit(second.batch.id, "synthetic-actor")

    reversed_batch = service.reverse(first.batch.id, "synthetic-actor")
    repeated_reversal = service.reverse(first.batch.id, "synthetic-actor")
    with closing(database.connect()) as conn:
        rows = conn.execute(
            "SELECT import_batch_id, reversed_at FROM transactions ORDER BY trade_date"
        ).fetchall()

    by_batch = {row["import_batch_id"]: row["reversed_at"] for row in rows}
    assert by_batch[first.batch.id] is not None
    assert by_batch[second.batch.id] is None
    assert repeated_reversal.reversed_at == reversed_batch.reversed_at


def test_positions_reconcile_and_preserve_unknown_cost_basis(settings: Settings) -> None:
    database, _, service, account_id = import_stack(settings)
    payload = (
        POSITION_HEADER
        + f"POSITION,{ACCOUNT_IDENTIFIER},SYNA,Invented A,ETF,2,12.345678901,24.69,,2026-02-01\n"
        + f"POSITION,{ACCOUNT_IDENTIFIER},SYNB,Invented B,CASH,10,1,10.00,10.00,2026-02-01\n"
        + f"TOTAL,{ACCOUNT_IDENTIFIER},,,,,,34.69,,\n"
    ).encode()

    preview = service.preview("manual_positions_csv_v1", account_id, payload)
    service.commit(preview.batch.id, "synthetic-actor")
    with closing(database.connect()) as conn:
        positions = conn.execute(
            "SELECT cost_basis_status, quantity FROM position_snapshots ORDER BY quantity"
        ).fetchall()
        prices = conn.execute("SELECT price, source, source_date FROM market_price_snapshots").fetchall()

    assert preview.batch.reconciliation_status == "matched"
    assert preview.calculated_total == "34.69"
    assert {row["cost_basis_status"] for row in positions} == {"unknown", "supplied"}
    assert "12.34567890" in {row["price"] for row in prices}
    assert {row["source"] for row in prices} == {"manual_positions_csv_v1"}
    assert {row["source_date"] for row in prices} == {"2026-02-01"}


def test_positions_reject_a_total_that_does_not_match_plan_valuation(settings: Settings) -> None:
    _, _, service, account_id = import_stack(settings)
    payload = (
        POSITION_HEADER
        + f"POSITION,{ACCOUNT_IDENTIFIER},SYNA,Invented A,ETF,2,10,21,,2026-02-01\n"
        + f"TOTAL,{ACCOUNT_IDENTIFIER},,,,,,21,,\n"
    ).encode()

    with pytest.raises(ImportValidationError) as captured:
        service.preview("manual_positions_csv_v1", account_id, payload)

    assert "market_value_price_mismatch" in {issue.code for issue in captured.value.issues}


def test_manual_adapters_reject_future_position_and_trade_dates() -> None:
    future = (datetime.now(UTC).date() + timedelta(days=1)).isoformat()
    position_payload = (
        POSITION_HEADER
        + f"POSITION,{ACCOUNT_IDENTIFIER},SYNA,Invented A,ETF,2,10,20,,{future}\n"
        + f"TOTAL,{ACCOUNT_IDENTIFIER},,,,,,20,,\n"
    ).encode()
    transaction_payload = transaction_csv(
        [f"{ACCOUNT_IDENTIFIER},{future},,DEPOSIT,,Invented deposit,,,,20,"]
    )

    for key, payload in (
        ("manual_positions_csv_v1", position_payload),
        ("manual_transactions_csv_v1", transaction_payload),
    ):
        with pytest.raises(ImportValidationError) as captured:
            adapter_for(key).parse(payload)
        assert "future_date" in {issue.code for issue in captured.value.issues}


def test_new_current_position_snapshot_supersedes_omitted_holdings(settings: Settings) -> None:
    database, _, service, account_id = import_stack(settings)
    first = (
        POSITION_HEADER
        + f"POSITION,{ACCOUNT_IDENTIFIER},SYNA,Invented A,ETF,2,10,20,,2026-02-01\n"
        + f"POSITION,{ACCOUNT_IDENTIFIER},SYNB,Invented B,ETF,3,10,30,,2026-02-01\n"
        + f"TOTAL,{ACCOUNT_IDENTIFIER},,,,,,50,,\n"
    ).encode()
    second = (
        POSITION_HEADER
        + f"POSITION,{ACCOUNT_IDENTIFIER},SYNA,Invented A,ETF,4,10,40,,2026-02-02\n"
        + f"TOTAL,{ACCOUNT_IDENTIFIER},,,,,,40,,\n"
    ).encode()

    first_preview = service.preview("manual_positions_csv_v1", account_id, first)
    service.commit(first_preview.batch.id, "synthetic-actor")
    second_preview = service.preview("manual_positions_csv_v1", account_id, second)
    service.commit(second_preview.batch.id, "synthetic-actor")
    with closing(database.connect()) as conn:
        active_symbols = [
            row[0]
            for row in conn.execute(
                """SELECT s.symbol_hash FROM position_snapshots p JOIN securities s ON s.id=p.security_id
                   WHERE p.account_id=? AND p.reversed_at IS NULL""",
                (account_id,),
            ).fetchall()
        ]
        reversed_count = conn.execute(
            "SELECT COUNT(*) FROM position_snapshots WHERE account_id=? AND reversed_at IS NOT NULL",
            (account_id,),
        ).fetchone()[0]

    assert len(active_symbols) == 1
    assert reversed_count == 2


def test_reversing_a_current_snapshot_restores_the_prior_snapshot(settings: Settings) -> None:
    database, _, service, account_id = import_stack(settings)
    first = (
        POSITION_HEADER
        + f"POSITION,{ACCOUNT_IDENTIFIER},SYNA,Invented A,ETF,2,10,20,,2026-02-01\n"
        + f"POSITION,{ACCOUNT_IDENTIFIER},SYNB,Invented B,ETF,3,10,30,,2026-02-01\n"
        + f"TOTAL,{ACCOUNT_IDENTIFIER},,,,,,50,,\n"
    ).encode()
    second = (
        POSITION_HEADER
        + f"POSITION,{ACCOUNT_IDENTIFIER},SYNA,Invented A,ETF,4,10,40,,2026-02-02\n"
        + f"TOTAL,{ACCOUNT_IDENTIFIER},,,,,,40,,\n"
    ).encode()

    first_preview = service.preview("manual_positions_csv_v1", account_id, first)
    service.commit(first_preview.batch.id, "synthetic-actor")
    second_preview = service.preview("manual_positions_csv_v1", account_id, second)
    service.commit(second_preview.batch.id, "synthetic-actor")
    service.reverse(second_preview.batch.id, "synthetic-actor")
    with closing(database.connect()) as conn:
        active_rows = conn.execute(
            """SELECT quantity, superseded_by_batch_id FROM position_snapshots
               WHERE account_id=? AND reversed_at IS NULL ORDER BY quantity""",
            (account_id,),
        ).fetchall()

    assert [Decimal(row["quantity"]) for row in active_rows] == [Decimal("2"), Decimal("3")]
    assert all(row["superseded_by_batch_id"] is None for row in active_rows)


def test_reversing_a_snapshot_chain_restores_the_nearest_eligible_ancestor(settings: Settings) -> None:
    database, _, service, account_id = import_stack(settings)
    payloads = [
        (
            POSITION_HEADER
            + f"POSITION,{ACCOUNT_IDENTIFIER},SYNA,Invented A,ETF,2,10,20,,2026-02-01\n"
            + f"TOTAL,{ACCOUNT_IDENTIFIER},,,,,,20,,\n"
        ).encode(),
        (
            POSITION_HEADER
            + f"POSITION,{ACCOUNT_IDENTIFIER},SYNA,Invented A,ETF,3,10,30,,2026-02-02\n"
            + f"TOTAL,{ACCOUNT_IDENTIFIER},,,,,,30,,\n"
        ).encode(),
        (
            POSITION_HEADER
            + f"POSITION,{ACCOUNT_IDENTIFIER},SYNA,Invented A,ETF,4,10,40,,2026-02-03\n"
            + f"TOTAL,{ACCOUNT_IDENTIFIER},,,,,,40,,\n"
        ).encode(),
    ]
    batches = []
    for payload in payloads:
        preview = service.preview("manual_positions_csv_v1", account_id, payload)
        service.commit(preview.batch.id, "synthetic-actor")
        batches.append(preview.batch.id)

    service.reverse(batches[1], "synthetic-actor")
    service.reverse(batches[2], "synthetic-actor")
    with closing(database.connect()) as conn:
        active_rows = conn.execute(
            """SELECT quantity, superseded_by_batch_id, manually_reversed_by_batch_id
               FROM position_snapshots WHERE account_id=? AND reversed_at IS NULL""",
            (account_id,),
        ).fetchall()

    assert len(active_rows) == 1
    assert Decimal(active_rows[0]["quantity"]) == Decimal("2")
    assert active_rows[0]["superseded_by_batch_id"] is None
    assert active_rows[0]["manually_reversed_by_batch_id"] is None


def test_reversing_only_the_latest_snapshot_restores_only_its_direct_predecessor(
    settings: Settings,
) -> None:
    database, _, service, account_id = import_stack(settings)
    payloads = [
        (
            POSITION_HEADER
            + f"POSITION,{ACCOUNT_IDENTIFIER},SYNA,Invented A,ETF,2,10,20,,2026-02-01\n"
            + f"TOTAL,{ACCOUNT_IDENTIFIER},,,,,,20,,\n"
        ).encode(),
        (
            POSITION_HEADER
            + f"POSITION,{ACCOUNT_IDENTIFIER},SYNA,Invented A,ETF,3,10,30,,2026-02-02\n"
            + f"TOTAL,{ACCOUNT_IDENTIFIER},,,,,,30,,\n"
        ).encode(),
        (
            POSITION_HEADER
            + f"POSITION,{ACCOUNT_IDENTIFIER},SYNA,Invented A,ETF,4,10,40,,2026-02-03\n"
            + f"TOTAL,{ACCOUNT_IDENTIFIER},,,,,,40,,\n"
        ).encode(),
    ]
    batches = []
    for payload in payloads:
        preview = service.preview("manual_positions_csv_v1", account_id, payload)
        service.commit(preview.batch.id, "synthetic-actor")
        batches.append(preview.batch.id)

    service.reverse(batches[2], "synthetic-actor")
    with closing(database.connect()) as conn:
        active_rows = conn.execute(
            """SELECT quantity, import_batch_id FROM position_snapshots
               WHERE account_id=? AND reversed_at IS NULL""",
            (account_id,),
        ).fetchall()

    assert len(active_rows) == 1
    assert Decimal(active_rows[0]["quantity"]) == Decimal("3")
    assert active_rows[0]["import_batch_id"] == batches[1]


def test_positions_reject_duplicate_symbols_before_reconciliation(settings: Settings) -> None:
    _, _, service, account_id = import_stack(settings)
    payload = (
        POSITION_HEADER
        + f"POSITION,{ACCOUNT_IDENTIFIER},SYNA,Invented A,ETF,1,10,10,,2026-02-01\n"
        + f"POSITION,{ACCOUNT_IDENTIFIER},syna,Invented A again,ETF,2,10,20,,2026-02-01\n"
        + f"TOTAL,{ACCOUNT_IDENTIFIER},,,,,,30,,\n"
    ).encode()

    with pytest.raises(ImportValidationError) as captured:
        service.preview("manual_positions_csv_v1", account_id, payload)

    assert "duplicate_security" in {issue.code for issue in captured.value.issues}


def test_transactions_allow_a_pending_future_settlement_date() -> None:
    trade_date = date.today().isoformat()
    settlement_date = (date.today() + timedelta(days=1)).isoformat()
    payload = transaction_csv(
        [
            f"{ACCOUNT_IDENTIFIER},{trade_date},{settlement_date},BUY,SYNA,Invented buy,2,10,,20,"
        ]
    )

    preview = adapter_for("manual_transactions_csv_v1").parse(payload)

    assert preview.records[0]["trade_date"] == trade_date
    assert preview.records[0]["settlement_date"] == settlement_date


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (b"\xff\xfe\x00bad", "invalid_encoding"),
        (b"Wrong,Headers\none,two\n", "invalid_headers"),
        (
            transaction_csv([f"{ACCOUNT_IDENTIFIER},2026-01-01,,DEPOSIT,,=2+3,,,,-99.99,"]),
            "formula_injection",
        ),
        (b"x" * (MAX_FILE_BYTES + 1), "file_too_large"),
    ],
)
def test_malformed_and_hostile_csv_is_rejected_without_echoing_values(
    payload: bytes, expected_code: str
) -> None:
    adapter = adapter_for("schwab_transactions_synthetic_v1")
    with pytest.raises(ImportValidationError) as captured:
        adapter.parse(payload)

    assert expected_code in {issue.code for issue in captured.value.issues}
    assert ACCOUNT_IDENTIFIER not in str(captured.value)
    assert "99.99" not in str(captured.value)


def test_row_limit_is_enforced() -> None:
    row = f"{ACCOUNT_IDENTIFIER},2026-01-01,,DEPOSIT,,Invented,,,,1.00,"
    payload = transaction_csv([row] * (MAX_ROWS + 1))

    with pytest.raises(ImportValidationError) as captured:
        adapter_for("schwab_transactions_synthetic_v1").parse(payload)

    assert {issue.code for issue in captured.value.issues} == {"too_many_rows"}


def test_validation_errors_and_logs_redact_source_values(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    _, _, service, account_id = import_stack(settings)
    payload = transaction_csv(
        [f"{ACCOUNT_IDENTIFIER},2026-01-01,,UNKNOWN,,Invented private value,,,,98765.43,"]
    )
    caplog.set_level(logging.DEBUG)

    with pytest.raises(ImportValidationError) as captured:
        service.preview("schwab_transactions_synthetic_v1", account_id, payload)

    combined = str(captured.value) + " ".join(record.getMessage() for record in caplog.records)
    assert ACCOUNT_IDENTIFIER not in combined
    assert "Invented private value" not in combined
    assert "98765.43" not in combined


def test_expanded_schema_survives_encrypted_backup_and_isolated_restore(
    settings: Settings, tmp_path: Path
) -> None:
    database, _, service, account_id = import_stack(settings)
    preview = service.preview(
        "schwab_transactions_synthetic_v1",
        account_id,
        transaction_csv([synthetic_transaction_rows()[0]]),
    )
    service.commit(preview.batch.id, "synthetic-actor")
    backups = BackupService(database, settings.backup_dir, settings.backup_vault())

    backup = backups.create()
    result = backups.restore_drill(backup)
    restored_path = tmp_path / "restored" / "northstar.db"
    backups.restore_to(backup, restored_path)
    restored = database.at_path(restored_path)
    with closing(restored.connect()) as conn:
        transaction_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    assert result.encrypted_records_verified >= 4
    assert transaction_count == 1
    assert restored.health()
