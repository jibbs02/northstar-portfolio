from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from northstar.data import Database, ImportBatch, PortfolioRepository
from northstar.data.database import utc_now
from northstar.data.portfolio import canonical_record, decrypted_json, encrypted_json
from northstar.imports import ImportPreview, ImportValidationError, ValidationIssue, adapter_for

PREVIEW_LIFETIME = timedelta(minutes=15)


@dataclass(frozen=True)
class PreviewSession:
    batch: ImportBatch
    records: list[dict[str, Any]]
    source_total: str | None
    calculated_total: str | None
    duplicate: bool = False


class PortfolioImportService:
    def __init__(self, database: Database, repository: PortfolioRepository) -> None:
        self.database = database
        self.repository = repository
        self.vault = database.vault

    def preview(self, adapter_key: str, account_id: str, payload: bytes) -> PreviewSession:
        self.purge_expired_previews()
        self.repository.get_account(account_id)
        adapter = adapter_for(adapter_key)
        digest = hashlib.sha256(payload).hexdigest()
        fingerprint = self.vault.blind_index(f"import-file:{adapter.key}", digest)
        existing = self.repository.find_batch(adapter.key, account_id, fingerprint)
        if existing is not None:
            if existing.status == "preview":
                return self.load_preview(existing.id, duplicate=True)
            return PreviewSession(existing, [], None, None, duplicate=True)
        try:
            parsed = adapter.parse(payload)
            self._validate_account(parsed, account_id)
        except ImportValidationError as exc:
            self._record_failure(adapter.key, account_id, fingerprint, len(payload), exc.issues)
            raise
        batch_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        expires = (now + PREVIEW_LIFETIME).isoformat(timespec="seconds")
        payload_value = {
            "records": parsed.records,
            "source_total": parsed.source_total,
            "calculated_total": parsed.calculated_total,
        }
        summary = {
            "source_total": parsed.source_total,
            "calculated_total": parsed.calculated_total,
        }
        with self.database.transaction() as conn:
            conn.execute(
                """
                INSERT INTO import_batches(
                    id, adapter_key, account_id, fingerprint, status, file_size_bytes,
                    row_count, accepted_count, rejected_count, reconciliation_status,
                    encrypted_summary, key_version, created_at
                ) VALUES (?, ?, ?, ?, 'preview', ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    adapter.key,
                    account_id,
                    fingerprint,
                    len(payload),
                    len(parsed.records),
                    len(parsed.records),
                    parsed.reconciliation_status,
                    encrypted_json(self.database, "import_batches", batch_id, "summary", summary),
                    self.vault.key_version,
                    now.isoformat(timespec="seconds"),
                ),
            )
            conn.execute(
                "INSERT INTO import_previews(batch_id, encrypted_payload, key_version, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    batch_id,
                    encrypted_json(
                        self.database,
                        "import_previews",
                        batch_id,
                        "payload",
                        payload_value,
                    ),
                    self.vault.key_version,
                    expires,
                ),
            )
        return PreviewSession(
            self.repository.batch(batch_id),
            parsed.records,
            parsed.source_total,
            parsed.calculated_total,
        )

    def load_preview(self, batch_id: str, *, duplicate: bool = False) -> PreviewSession:
        self.purge_expired_previews()
        with self.database.transaction() as conn:
            row = conn.execute(
                """SELECT b.*, p.encrypted_payload, p.expires_at
                   FROM import_batches b JOIN import_previews p ON p.batch_id=b.id
                   WHERE b.id=? AND b.status='preview'""",
                (batch_id,),
            ).fetchone()
            if row is None:
                raise LookupError("Import preview is unavailable")
            value = decrypted_json(
                self.database,
                "import_previews",
                batch_id,
                "payload",
                row["encrypted_payload"],
            )
        return PreviewSession(
            self.repository.batch(batch_id),
            value["records"],
            value["source_total"],
            value["calculated_total"],
            duplicate,
        )

    def commit(self, batch_id: str, actor: str) -> ImportBatch:
        self.purge_expired_previews()
        imported_at = utc_now()
        with self.database.transaction() as conn:
            row = conn.execute(
                """SELECT b.*, p.encrypted_payload, p.expires_at
                   FROM import_batches b JOIN import_previews p ON p.batch_id=b.id
                   WHERE b.id=? AND b.status='preview'""",
                (batch_id,),
            ).fetchone()
            if row is None:
                batch = conn.execute("SELECT status FROM import_batches WHERE id=?", (batch_id,)).fetchone()
                if batch and batch["status"] in {"committed", "reversed"}:
                    row = None
                else:
                    raise LookupError("Import preview is unavailable")
            if row is None:
                pass
            else:
                preview = decrypted_json(
                    self.database,
                    "import_previews",
                    batch_id,
                    "payload",
                    row["encrypted_payload"],
                )
                for record in preview["records"]:
                    self._insert_record(conn, row, record, imported_at)
                position_dates = {
                    str(record["source_date"])
                    for record in preview["records"]
                    if record["kind"] == "position" and record["source_date"] is not None
                }
                if position_dates:
                    self._supersede_prior_positions(
                        conn,
                        account_id=str(row["account_id"]),
                        source_date=position_dates.pop(),
                        current_batch_id=batch_id,
                        superseded_at=imported_at,
                    )
                conn.execute("DELETE FROM import_previews WHERE batch_id=?", (batch_id,))
                conn.execute(
                    "UPDATE import_batches SET status='committed', imported_at=? WHERE id=?",
                    (imported_at, batch_id),
                )
                self._audit(
                    conn,
                    "import.committed",
                    actor,
                    batch_id,
                    {"adapter": row["adapter_key"], "record_count": row["row_count"]},
                )
        return self.repository.batch(batch_id)

    def reverse(self, batch_id: str, actor: str) -> ImportBatch:
        reversed_at = utc_now()
        tables = {
            "transaction": "transactions",
            "tax_lot": "tax_lots",
            "position_snapshot": "position_snapshots",
            "market_price_snapshot": "market_price_snapshots",
        }
        with self.database.transaction() as conn:
            batch = conn.execute("SELECT status FROM import_batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise LookupError("Import batch was not found")
            already_reversed = batch["status"] == "reversed"
            if not already_reversed and batch["status"] != "committed":
                raise ValueError("Only a committed import can be reversed")
            if not already_reversed:
                records = conn.execute(
                    "SELECT record_type, record_id FROM import_batch_records WHERE batch_id=?",
                    (batch_id,),
                ).fetchall()
                for record in records:
                    table = tables[record["record_type"]]
                    conn.execute(
                        f"UPDATE {table} SET reversed_at=? "  # noqa: S608 -- fixed allow-list
                        "WHERE id=? AND import_batch_id=? AND reversed_at IS NULL",
                        (reversed_at, record["record_id"], batch_id),
                    )
                conn.execute(
                    """UPDATE position_snapshots SET manually_reversed_by_batch_id=?
                       WHERE import_batch_id=?""",
                    (batch_id, batch_id),
                )
                self._restore_superseded_positions(conn, batch_id)
                conn.execute(
                    "UPDATE import_batches SET status='reversed', reversed_at=? WHERE id=?",
                    (reversed_at, batch_id),
                )
                self._audit(
                    conn,
                    "import.reversed",
                    actor,
                    batch_id,
                    {"record_count": len(records)},
                )
        return self.repository.batch(batch_id)

    def purge_expired_previews(self) -> int:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self.database.transaction() as conn:
            rows = conn.execute("SELECT batch_id FROM import_previews WHERE expires_at<=?", (now,)).fetchall()
            batch_ids = [row["batch_id"] for row in rows]
            for batch_id in batch_ids:
                conn.execute("DELETE FROM import_previews WHERE batch_id=?", (batch_id,))
                conn.execute(
                    "UPDATE import_batches SET status='failed', rejected_count=row_count "
                    "WHERE id=? AND status='preview'",
                    (batch_id,),
                )
        return len(batch_ids)

    def _validate_account(self, preview: ImportPreview, account_id: str) -> None:
        if len(preview.account_identifiers) != 1:
            raise ImportValidationError([ValidationIssue(None, "Account Number", "ambiguous_account")])
        candidate = next(iter(preview.account_identifiers))
        if not self.repository.account_identifier_matches(account_id, candidate):
            raise ImportValidationError([ValidationIssue(None, "Account Number", "account_mismatch")])

    def _record_failure(
        self,
        adapter_key: str,
        account_id: str,
        fingerprint: str,
        file_size: int,
        issues: list[ValidationIssue],
    ) -> None:
        batch_id = str(uuid.uuid4())
        safe_issues = [{"row": issue.row, "field": issue.field, "code": issue.code} for issue in issues[:100]]
        with self.database.transaction() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO import_batches(
                    id, adapter_key, account_id, fingerprint, status, file_size_bytes,
                    row_count, accepted_count, rejected_count, reconciliation_status,
                    encrypted_summary, key_version, created_at
                ) VALUES (?, ?, ?, ?, 'failed', ?, 0, 0, ?, 'not_applicable', ?, ?, ?)
                """,
                (
                    batch_id,
                    adapter_key,
                    account_id,
                    fingerprint,
                    file_size,
                    len(issues),
                    encrypted_json(
                        self.database,
                        "import_batches",
                        batch_id,
                        "summary",
                        {"issues": safe_issues},
                    ),
                    self.vault.key_version,
                    utc_now(),
                ),
            )

    def _insert_record(self, conn: Any, batch: Any, record: dict[str, Any], imported_at: str) -> None:
        security_id = None
        if record.get("symbol"):
            security_id = self._security(
                conn,
                str(record["symbol"]),
                str(record.get("description") or record["symbol"]),
                str(record.get("instrument_type") or "other"),
            )
        if record["kind"] == "transaction":
            self._insert_transaction(conn, batch, record, security_id, imported_at)
        elif record["kind"] == "position":
            self._insert_position(conn, batch, record, security_id)
        else:
            raise ValueError("Unsupported normalized import record")

    def _security(self, conn: Any, symbol: str, name: str, instrument_type: str) -> str:
        normalized = symbol.strip().upper()
        symbol_hash = self.vault.blind_index("security-symbol", normalized)
        existing = conn.execute("SELECT id FROM securities WHERE symbol_hash=?", (symbol_hash,)).fetchone()
        if existing:
            return str(existing["id"])
        security_id = str(uuid.uuid4())
        now = utc_now()
        asset_class = {
            "equity": "equity",
            "etf": "equity",
            "mutual_fund": "equity",
            "bond": "fixed_income",
            "cash": "cash",
            "bitcoin": "bitcoin",
        }.get(instrument_type, "other")
        conn.execute(
            """INSERT INTO securities(
                   id, instrument_type, currency, encrypted_symbol, symbol_hash,
                   encrypted_name, key_version, created_at, updated_at, asset_class
               ) VALUES (?, ?, 'USD', ?, ?, ?, ?, ?, ?, ?)""",
            (
                security_id,
                instrument_type,
                encrypted_json(self.database, "securities", security_id, "symbol", normalized),
                symbol_hash,
                encrypted_json(self.database, "securities", security_id, "name", name),
                self.vault.key_version,
                now,
                now,
                asset_class,
            ),
        )
        return security_id

    def _insert_transaction(
        self,
        conn: Any,
        batch: Any,
        record: dict[str, Any],
        security_id: str | None,
        imported_at: str,
    ) -> None:
        record_id = str(uuid.uuid4())
        source_hash = self.vault.blind_index(
            f"source-record:{batch['adapter_key']}", canonical_record(record)
        )
        trade_type = record["transaction_type"]
        cost_basis = record["cost_basis"]
        fees = record["fees"]
        basis_status = (
            "supplied"
            if cost_basis is not None
            else "unknown"
            if trade_type in {"buy", "sell", "reinvestment"}
            else "not_applicable"
        )
        fees_status = (
            "supplied"
            if fees is not None
            else "unknown"
            if trade_type in {"buy", "sell", "reinvestment"}
            else "not_applicable"
        )
        description = record.get("description")
        conn.execute(
            """INSERT INTO transactions(
                   id, account_id, security_id, transaction_type, trade_date,
                   settlement_date, imported_at, quantity, price, gross_amount,
                   gross_amount_status, fees, net_amount, net_amount_status,
                   cost_basis, cost_basis_status, fees_status,
                   encrypted_description, encrypted_notes, source_record_hash,
                   import_batch_id, key_version
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'unknown', ?, ?, ?, ?, ?, ?, ?,
                         NULL, ?, ?, ?)""",
            (
                record_id,
                batch["account_id"],
                security_id,
                trade_type,
                record["trade_date"],
                record["settlement_date"],
                imported_at,
                record["quantity"],
                record["price"],
                fees,
                record["amount"],
                "supplied" if record["amount"] is not None else "unknown",
                cost_basis,
                basis_status,
                fees_status,
                encrypted_json(self.database, "transactions", record_id, "description", description)
                if description
                else None,
                source_hash,
                batch["id"],
                self.vault.key_version,
            ),
        )
        self._map(conn, batch["id"], "transaction", record_id)
        if trade_type in {"buy", "reinvestment"} and security_id and record["quantity"]:
            lot_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO tax_lots(
                       id, account_id, security_id, acquired_date, quantity, cost_basis,
                       cost_basis_status, import_batch_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    lot_id,
                    batch["account_id"],
                    security_id,
                    record["trade_date"],
                    record["quantity"],
                    cost_basis,
                    "supplied" if cost_basis is not None else "unknown",
                    batch["id"],
                ),
            )
            self._map(conn, batch["id"], "tax_lot", lot_id)

    def _insert_position(
        self,
        conn: Any,
        batch: Any,
        record: dict[str, Any],
        security_id: str | None,
    ) -> None:
        if security_id is None:
            raise ValueError("Position security is required")
        observed_at = datetime.combine(
            date.fromisoformat(record["source_date"]), time.min, UTC
        ).isoformat(timespec="seconds")
        position_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO position_snapshots(
                   id, account_id, security_id, as_of_utc, source_date, quantity,
                   market_value, market_value_status, cost_basis, cost_basis_status,
                   import_batch_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                position_id,
                batch["account_id"],
                security_id,
                observed_at,
                record["source_date"],
                record["quantity"],
                record["market_value"],
                "supplied" if record["market_value"] is not None else "unknown",
                record["cost_basis"],
                "supplied" if record["cost_basis"] is not None else "unknown",
                batch["id"],
            ),
        )
        self._map(conn, batch["id"], "position_snapshot", position_id)
        price_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO market_price_snapshots(
                   id, security_id, observed_at_utc, source_date, price, source, import_batch_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                price_id,
                security_id,
                observed_at,
                record["source_date"],
                record["price"],
                batch["adapter_key"],
                batch["id"],
            ),
        )
        self._map(conn, batch["id"], "market_price_snapshot", price_id)

    @staticmethod
    def _map(conn: Any, batch_id: str, record_type: str, record_id: str) -> None:
        conn.execute(
            "INSERT INTO import_batch_records(batch_id, record_type, record_id) VALUES (?, ?, ?)",
            (batch_id, record_type, record_id),
        )

    @staticmethod
    def _supersede_prior_positions(
        conn: Any,
        *,
        account_id: str,
        source_date: str,
        current_batch_id: str,
        superseded_at: str,
    ) -> None:
        """A current manual snapshot is authoritative for its account through its source date."""
        conn.execute(
            """UPDATE position_snapshots SET reversed_at=?, superseded_by_batch_id=?
               WHERE account_id=? AND import_batch_id<>? AND source_date<=? AND reversed_at IS NULL""",
            (superseded_at, current_batch_id, account_id, current_batch_id, source_date),
        )

    @staticmethod
    def _restore_superseded_positions(conn: Any, superseding_batch_id: str) -> None:
        """Restore an eligible supersession ancestor when no later snapshot remains active."""
        conn.execute(
            """WITH RECURSIVE predecessor_batches(batch_id, depth) AS (
                   SELECT ?, 0
                   UNION
                   SELECT prior.import_batch_id, chain.depth + 1
                   FROM predecessor_batches AS chain
                   JOIN position_snapshots AS prior
                     ON prior.superseded_by_batch_id=chain.batch_id
                   WHERE prior.import_batch_id IS NOT NULL
               ),
               eligible_positions AS (
                   SELECT prior.id, prior.account_id, chain.depth
                   FROM predecessor_batches AS chain
                   JOIN position_snapshots AS prior ON prior.import_batch_id=chain.batch_id
                   WHERE chain.depth>0
                     AND prior.manually_reversed_by_batch_id IS NULL
                     AND NOT EXISTS (
                         SELECT 1 FROM position_snapshots AS newer
                         WHERE newer.account_id=prior.account_id
                           AND newer.reversed_at IS NULL
                           AND newer.source_date>=prior.source_date
                     )
               ),
               nearest_depth AS (
                   SELECT account_id, MIN(depth) AS depth
                   FROM eligible_positions
                   GROUP BY account_id
               )
               UPDATE position_snapshots AS prior
               SET reversed_at=NULL, superseded_by_batch_id=NULL
               WHERE prior.id IN (
                   SELECT eligible.id
                   FROM eligible_positions AS eligible
                   JOIN nearest_depth AS nearest
                     ON nearest.account_id=eligible.account_id AND nearest.depth=eligible.depth
               )
            """,
            (superseding_batch_id,),
        )

    def _audit(
        self,
        conn: Any,
        event_type: str,
        actor: str,
        subject: str,
        metadata: dict[str, str | int],
    ) -> None:
        conn.execute(
            """INSERT INTO audit_events(
                   event_type, actor_hash, subject_hash, metadata_json, created_at
               ) VALUES (?, ?, ?, ?, ?)""",
            (
                event_type,
                self.vault.blind_index("actor", actor),
                self.vault.blind_index("subject", subject),
                json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                utc_now(),
            ),
        )
