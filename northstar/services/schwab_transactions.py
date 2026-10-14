from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from northstar.data.connections import ProviderConnectionRepository
from northstar.data.database import Database, utc_now
from northstar.data.portfolio import encrypted_json
from northstar.domain import DecimalKind, canonical_decimal
from northstar.integrations import SchwabOAuthError, SchwabTransaction, SchwabTransactionItem
from northstar.services.schwab_snapshots import (
    SchwabSnapshotService,
    SnapshotMappingReference,
    SnapshotMappingSet,
    SnapshotRefreshError,
    instrument_type_compatible,
)

TRANSACTION_PROVENANCE = "schwab_synthetic_transactions_v1"
SCHWAB_HTTP_TRANSACTION_PROVENANCE = "schwab_http_transactions_v1"
TRANSACTION_PROVENANCES = frozenset(
    {TRANSACTION_PROVENANCE, SCHWAB_HTTP_TRANSACTION_PROVENANCE}
)
TRANSACTION_LOAD_TIMEOUT = timedelta(seconds=30)
TRANSACTION_MAX_RANGE = timedelta(days=365)


class TransactionRefreshError(ValueError):
    """An allowlisted, privacy-safe transaction refresh failure."""


@dataclass(frozen=True)
class TransactionPublication:
    id: str
    window_start_date: date
    window_end_date: date
    imported_at_utc: datetime
    published_at: datetime
    provenance: str
    reconciliation_status: str
    mapped_account_count: int
    transaction_count: int


@dataclass(frozen=True)
class _ClassifiedTransaction:
    mapping: SnapshotMappingReference
    provider: SchwabTransaction
    transaction_type: str
    security_item: SchwabTransactionItem | None
    quantity: str | None
    price: str | None
    fees: str | None
    fees_status: str
    provider_key_index: str
    source_record_hash: str


class SchwabTransactionService:
    """Normalize complete mapped-account windows and publish one atomic checkpoint."""

    def __init__(self, database: Database, connections: ProviderConnectionRepository) -> None:
        self.database = database
        self.connections = connections
        self._mapping_guard = SchwabSnapshotService(database, connections)
        self._locks: dict[str, asyncio.Lock] = {}

    async def refresh(
        self,
        *,
        connection_id: str,
        actor: str,
        start_date: date,
        end_date: date,
        load_transactions: Callable[
            [str, date, date], Awaitable[tuple[SchwabTransaction, ...]]
        ],
        provenance: str = TRANSACTION_PROVENANCE,
        mapping_set: SnapshotMappingSet | None = None,
    ) -> TransactionPublication:
        _window(start_date, end_date)
        if provenance not in TRANSACTION_PROVENANCES:
            raise ValueError("Transaction provenance is not allowed")
        lock = self._locks.setdefault(connection_id, asyncio.Lock())
        if lock.locked():
            raise TransactionRefreshError("transaction_refresh_in_progress")
        async with lock:
            sync_run = self.connections.start_sync(connection_id, actor=actor)
            records_seen = 0
            try:
                pinned = mapping_set or self._mapping_guard.capture_mapping_set(connection_id)
                self._mapping_guard._validate_mapping_set(connection_id, pinned)
                if not pinned.mappings:
                    raise TransactionRefreshError("transaction_no_mapped_accounts")
                loaded = await self._load_all(
                    load_transactions, pinned.mappings, start_date, end_date
                )
                records_seen = sum(len(records) for records in loaded)
                return self._publish(
                    connection_id,
                    sync_run,
                    pinned,
                    loaded,
                    start_date=start_date,
                    end_date=end_date,
                    provenance=provenance,
                )
            except TransactionRefreshError as exc:
                self.connections.finish_sync(
                    sync_run,
                    succeeded=False,
                    records_seen=records_seen,
                    records_written=0,
                    error_code=str(exc),
                )
                raise
            except SnapshotRefreshError as exc:
                self.connections.finish_sync(
                    sync_run,
                    succeeded=False,
                    records_seen=records_seen,
                    records_written=0,
                    error_code=str(exc).replace("snapshot_", "transaction_", 1),
                )
                raise TransactionRefreshError(
                    str(exc).replace("snapshot_", "transaction_", 1)
                ) from None
            except SchwabOAuthError as exc:
                self.connections.finish_sync(
                    sync_run,
                    succeeded=False,
                    records_seen=records_seen,
                    records_written=0,
                    error_code=exc.code,
                )
                raise TransactionRefreshError(exc.code) from None
            except TimeoutError:
                self.connections.finish_sync(
                    sync_run,
                    succeeded=False,
                    records_seen=records_seen,
                    records_written=0,
                    error_code="provider_timeout",
                )
                raise TransactionRefreshError("provider_timeout") from None
            except asyncio.CancelledError:
                self.connections.finish_sync(
                    sync_run,
                    succeeded=False,
                    records_seen=records_seen,
                    records_written=0,
                    error_code="transaction_refresh_cancelled",
                )
                raise
            except Exception:
                self.connections.finish_sync(
                    sync_run,
                    succeeded=False,
                    records_seen=records_seen,
                    records_written=0,
                    error_code="provider_transaction_refresh_failed",
                )
                raise TransactionRefreshError("provider_transaction_refresh_failed") from None

    async def _load_all(
        self,
        loader: Callable[[str, date, date], Awaitable[tuple[SchwabTransaction, ...]]],
        mappings: tuple[SnapshotMappingReference, ...],
        start_date: date,
        end_date: date,
    ) -> tuple[tuple[SchwabTransaction, ...], ...]:
        tasks = [
            asyncio.create_task(self._load_one(loader, mapping, start_date, end_date))
            for mapping in mappings
        ]
        try:
            async with asyncio.timeout(TRANSACTION_LOAD_TIMEOUT.total_seconds()):
                return tuple(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    @staticmethod
    async def _load_one(
        loader: Callable[[str, date, date], Awaitable[tuple[SchwabTransaction, ...]]],
        mapping: SnapshotMappingReference,
        start_date: date,
        end_date: date,
    ) -> tuple[SchwabTransaction, ...]:
        result = await loader(mapping.provider_account_hash, start_date, end_date)
        if not isinstance(result, tuple) or any(
            not isinstance(item, SchwabTransaction)
            or item.provider_account_hash != mapping.provider_account_hash
            for item in result
        ):
            raise TransactionRefreshError("provider_validation_account_identity_mechanics")
        return result

    def current(self, connection_id: str) -> TransactionPublication | None:
        with closing(self.database.connect()) as conn:
            row = conn.execute(
                """SELECT * FROM provider_transaction_checkpoints WHERE connection_id=?
                   AND status='published' ORDER BY sync_run_id DESC LIMIT 1""",
                (connection_id,),
            ).fetchone()
        if row is None:
            return None
        return TransactionPublication(
            id=str(row["id"]),
            window_start_date=date.fromisoformat(row["window_start_date"]),
            window_end_date=date.fromisoformat(row["window_end_date"]),
            imported_at_utc=datetime.fromisoformat(row["imported_at_utc"]),
            published_at=datetime.fromisoformat(row["published_at"]),
            provenance=str(row["provenance"]),
            reconciliation_status=str(row["reconciliation_status"]),
            mapped_account_count=int(row["mapped_account_count"]),
            transaction_count=int(row["transaction_count"]),
        )

    def _publish(
        self,
        connection_id: str,
        sync_run: int,
        mapping_set: SnapshotMappingSet,
        loaded: tuple[tuple[SchwabTransaction, ...], ...],
        *,
        start_date: date,
        end_date: date,
        provenance: str,
    ) -> TransactionPublication:
        if len(loaded) != len(mapping_set.mappings):
            raise TransactionRefreshError("provider_transaction_partial_data")
        classified: list[_ClassifiedTransaction] = []
        for mapping, records in zip(mapping_set.mappings, loaded, strict=True):
            for record in records:
                classified.append(
                    self._classify(mapping, record, start_date=start_date, end_date=end_date)
                )
        identities = [
            (item.mapping.id, item.provider_key_index) for item in classified
        ]
        if len(set(identities)) != len(identities):
            raise TransactionRefreshError("provider_transaction_duplicate")
        checkpoint_id = str(uuid.uuid4())
        imported_at = max(
            (item.provider.imported_at_utc for item in classified),
            default=datetime.now(UTC),
        )
        with self.database.transaction() as conn:
            self._mapping_guard._validate_mapping_set(connection_id, mapping_set, conn=conn)
            self._reject_replay(
                conn,
                connection_id,
                start_date,
                end_date,
                classified,
            )
            rows: list[tuple[_ClassifiedTransaction, str]] = []
            written = 0
            for item in classified:
                transaction_id, inserted = self._upsert_transaction(conn, connection_id, item)
                rows.append((item, transaction_id))
                written += int(inserted)
            now = utc_now()
            metadata = json.dumps(
                {
                    "classification": "strict",
                    "deduplication": "provider_identity_and_fingerprint",
                    "raw_response_stored": False,
                    "window_complete": True,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            conn.execute(
                """INSERT INTO provider_transaction_checkpoints(
                       id, connection_id, sync_run_id, status, window_start_date,
                       window_end_date, imported_at_utc, published_at, provenance,
                       reconciliation_status, mapped_account_count, transaction_count,
                       audit_metadata_json
                   ) VALUES (?, ?, ?, 'published', ?, ?, ?, ?, ?, 'matched', ?, ?, ?)""",
                (
                    checkpoint_id,
                    connection_id,
                    sync_run,
                    start_date.isoformat(),
                    end_date.isoformat(),
                    imported_at.isoformat(),
                    now,
                    provenance,
                    len(mapping_set.mappings),
                    len(rows),
                    metadata,
                ),
            )
            conn.executemany(
                """INSERT INTO provider_transaction_checkpoint_records(
                       checkpoint_id, mapping_id, transaction_id,
                       provider_transaction_key_index, source_record_hash
                   ) VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        checkpoint_id,
                        item.mapping.id,
                        transaction_id,
                        item.provider_key_index,
                        item.source_record_hash,
                    )
                    for item, transaction_id in rows
                ],
            )
            conn.execute(
                """UPDATE sync_runs SET status='succeeded', records_seen=?, records_written=?,
                   error_code=NULL, finished_at=? WHERE id=? AND status='running'""",
                (len(rows), written, now, sync_run),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise TransactionRefreshError("transaction_sync_state_changed")
            conn.execute(
                "UPDATE provider_connections SET last_successful_sync_at=?, updated_at=? WHERE id=?",
                (now, now, connection_id),
            )
            conn.execute(
                """INSERT INTO audit_events(
                       event_type, actor_hash, subject_hash, metadata_json, created_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    "schwab_transactions_published",
                    self.database.vault.blind_index("actor", "transaction-service"),
                    self.database.vault.blind_index("subject", connection_id),
                    json.dumps(
                        {
                            "mapped_accounts": len(mapping_set.mappings),
                            "raw_response_stored": False,
                            "transactions": len(rows),
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    now,
                ),
            )
        return self.current(connection_id) or (_ for _ in ()).throw(
            AssertionError("published transaction checkpoint missing")
        )

    def _classify(
        self,
        mapping: SnapshotMappingReference,
        record: SchwabTransaction,
        *,
        start_date: date,
        end_date: date,
    ) -> _ClassifiedTransaction:
        if record.provider_status != "VALID" or record.activity_type in {
            "ACTIVITY_CORRECTION",
            "ORDER_ACTION",
            "UNKNOWN",
        }:
            raise TransactionRefreshError("provider_transaction_semantics_unsupported")
        source_date = record.provider_source_time_utc.date()
        if not start_date <= source_date <= end_date:
            raise TransactionRefreshError("provider_transaction_window_mismatch")
        security_items = tuple(
            item for item in record.items if item.instrument_type not in {None, "cash"}
        )
        fee_items = tuple(item for item in record.items if item.fee_type is not None)
        transaction_type: str
        security_item: SchwabTransactionItem | None = None
        quantity: str | None = None
        price: str | None = None
        if fee_items and len(fee_items) == len(record.items) and record.net_amount < 0:
            transaction_type = "fee"
        elif record.provider_type == "TRADE":
            if len(security_items) != 1:
                raise TransactionRefreshError("provider_transaction_semantics_unsupported")
            security_item = security_items[0]
            if (
                security_item.amount is None
                or security_item.amount == 0
                or security_item.price is None
                or security_item.price < 0
            ):
                raise TransactionRefreshError("provider_transaction_semantics_unsupported")
            if record.net_amount < 0 and security_item.position_effect == "OPENING":
                transaction_type = "buy"
            elif record.net_amount > 0 and security_item.position_effect == "CLOSING":
                transaction_type = "sell"
            elif record.net_amount == 0 and security_item.position_effect == "AUTOMATIC":
                transaction_type = "reinvestment"
            else:
                raise TransactionRefreshError("provider_transaction_semantics_unsupported")
            quantity = canonical_decimal(
                abs(security_item.amount), DecimalKind.QUANTITY, allow_negative=False
            )
            price = canonical_decimal(
                security_item.price, DecimalKind.PRICE, allow_negative=False
            )
        elif record.provider_type == "DIVIDEND_OR_INTEREST" and record.net_amount > 0:
            if len(security_items) == 1:
                transaction_type = "dividend"
                security_item = security_items[0]
            elif not security_items and not record.items:
                transaction_type = "interest"
            else:
                raise TransactionRefreshError("provider_transaction_semantics_unsupported")
        elif record.provider_type in {"ACH_RECEIPT", "CASH_RECEIPT", "WIRE_IN"}:
            transaction_type = "deposit"
            self._require_cash_flow(record, positive=True, security_items=security_items)
        elif record.provider_type in {"ACH_DISBURSEMENT", "CASH_DISBURSEMENT", "WIRE_OUT"}:
            transaction_type = "withdrawal"
            self._require_cash_flow(record, positive=False, security_items=security_items)
        elif record.provider_type in {"RECEIVE_AND_DELIVER", "ELECTRONIC_FUND", "JOURNAL"}:
            if record.activity_type != "TRANSFER" or security_items or record.net_amount == 0:
                raise TransactionRefreshError("provider_transaction_semantics_unsupported")
            transaction_type = "transfer_in" if record.net_amount > 0 else "transfer_out"
        else:
            raise TransactionRefreshError("provider_transaction_semantics_unsupported")
        fees = None
        fees_status = "not_applicable"
        if transaction_type in {"buy", "sell", "reinvestment"}:
            if fee_items:
                if any(item.amount is None for item in fee_items):
                    raise TransactionRefreshError("provider_transaction_semantics_unsupported")
                fees = canonical_decimal(
                    sum((abs(item.amount or Decimal()) for item in fee_items), Decimal()),
                    DecimalKind.AMOUNT,
                    allow_negative=False,
                )
                fees_status = "supplied"
            else:
                fees_status = "unknown"
        provider_key_index = self.database.vault.blind_index(
            "provider-transaction:schwab",
            f"{mapping.external_account_hash_index}:{record.provider_transaction_key}",
        )
        fingerprint_payload = json.dumps(
            {
                "activity": record.activity_type,
                "fees": fees,
                "fees_status": fees_status,
                "fee_types": sorted(item.fee_type for item in fee_items if item.fee_type),
                "instrument_key": security_item.provider_instrument_key if security_item else None,
                "instrument_type": security_item.instrument_type if security_item else None,
                "net": canonical_decimal(
                    record.net_amount, DecimalKind.AMOUNT, allow_negative=True
                ),
                "price": price,
                "provider_key": record.provider_transaction_key,
                "provider_type": record.provider_type,
                "quantity": quantity,
                "position_effect": security_item.position_effect if security_item else None,
                "settlement": record.settlement_date.isoformat()
                if record.settlement_date
                else None,
                "source": record.provider_source_time_utc.isoformat(),
                "symbol": security_item.symbol if security_item else None,
                "trade": record.trade_date.isoformat() if record.trade_date else None,
                "transaction_type": transaction_type,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        source_record_hash = self.database.vault.blind_index(
            "source-record:schwab_transactions_http_v1", fingerprint_payload
        )
        return _ClassifiedTransaction(
            mapping=mapping,
            provider=record,
            transaction_type=transaction_type,
            security_item=security_item,
            quantity=quantity,
            price=price,
            fees=fees,
            fees_status=fees_status,
            provider_key_index=provider_key_index,
            source_record_hash=source_record_hash,
        )

    @staticmethod
    def _require_cash_flow(
        record: SchwabTransaction,
        *,
        positive: bool,
        security_items: tuple[SchwabTransactionItem, ...],
    ) -> None:
        valid_sign = record.net_amount > 0 if positive else record.net_amount < 0
        if not valid_sign or security_items:
            raise TransactionRefreshError("provider_transaction_semantics_unsupported")

    def _upsert_transaction(
        self,
        conn: Any,
        connection_id: str,
        item: _ClassifiedTransaction,
    ) -> tuple[str, bool]:
        existing = conn.execute(
            """SELECT id, source_record_hash FROM transactions
               WHERE account_id=? AND provider_transaction_key_index=?""",
            (item.mapping.account_id, item.provider_key_index),
        ).fetchone()
        if existing is not None:
            if str(existing["source_record_hash"]) != item.source_record_hash:
                raise TransactionRefreshError("provider_transaction_changed")
            return str(existing["id"]), False
        security_id: str | None = None
        if item.security_item is not None:
            security_id = self._resolve_security(conn, connection_id, item.security_item)
        transaction_id = str(uuid.uuid4())
        provider = item.provider
        trade_date = (provider.trade_date or provider.provider_source_time_utc).date().isoformat()
        settlement_date = (
            provider.settlement_date.date().isoformat() if provider.settlement_date else None
        )
        net_amount = canonical_decimal(
            provider.net_amount, DecimalKind.AMOUNT, allow_negative=True
        )
        cost_basis_status = (
            "unknown"
            if item.transaction_type in {"buy", "sell", "reinvestment"}
            else "not_applicable"
        )
        conn.execute(
            """INSERT INTO transactions(
                   id, account_id, security_id, transaction_type, trade_date,
                   settlement_date, imported_at, quantity, price, gross_amount,
                   gross_amount_status, fees, net_amount, net_amount_status,
                   cost_basis, cost_basis_status, fees_status, encrypted_description,
                   encrypted_notes, source_record_hash, import_batch_id, key_version,
                   provider_sync_batch_id, encrypted_provider_transaction_key,
                   provider_transaction_key_index, provider_source_time_utc
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'unknown', ?, ?, 'supplied',
                         NULL, ?, ?, NULL, NULL, ?, NULL, ?, NULL, ?, ?, ?)""",
            (
                transaction_id,
                item.mapping.account_id,
                security_id,
                item.transaction_type,
                trade_date,
                settlement_date,
                provider.imported_at_utc.isoformat(),
                item.quantity,
                item.price,
                item.fees,
                net_amount,
                cost_basis_status,
                item.fees_status,
                item.source_record_hash,
                self.database.vault.key_version,
                encrypted_json(
                    self.database,
                    "transactions",
                    transaction_id,
                    "provider_transaction_key",
                    provider.provider_transaction_key,
                ),
                item.provider_key_index,
                provider.provider_source_time_utc.isoformat(),
            ),
        )
        return transaction_id, True

    def _resolve_security(
        self, conn: Any, connection_id: str, item: SchwabTransactionItem
    ) -> str:
        if (
            item.symbol is None
            or item.provider_instrument_key is None
            or item.instrument_type is None
        ):
            raise TransactionRefreshError("provider_unknown_security")
        security = conn.execute(
            "SELECT id, instrument_type FROM securities WHERE symbol_hash=?",
            (self.database.vault.blind_index("security-symbol", item.symbol.upper()),),
        ).fetchone()
        if security is None:
            raise TransactionRefreshError("provider_unknown_security")
        if not instrument_type_compatible(item.instrument_type, str(security["instrument_type"])):
            raise TransactionRefreshError("provider_instrument_identity_conflict")
        instrument_key = self.database.vault.blind_index(
            "provider-instrument:schwab", item.provider_instrument_key
        )
        binding = conn.execute(
            """SELECT security_id FROM provider_instrument_bindings
               WHERE connection_id=? AND provider_instrument_key_index=?""",
            (connection_id, instrument_key),
        ).fetchone()
        if binding is not None and str(binding["security_id"]) != str(security["id"]):
            raise TransactionRefreshError("provider_instrument_identity_conflict")
        if binding is None:
            now = utc_now()
            conn.execute(
                """INSERT INTO provider_instrument_bindings(
                       connection_id, provider_instrument_key_index, security_id,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (connection_id, instrument_key, str(security["id"]), now, now),
            )
        return str(security["id"])

    @staticmethod
    def _reject_replay(
        conn: Any,
        connection_id: str,
        start_date: date,
        end_date: date,
        classified: list[_ClassifiedTransaction],
    ) -> None:
        current = conn.execute(
            """SELECT id FROM provider_transaction_checkpoints
               WHERE connection_id=? AND window_start_date=? AND window_end_date=?
               ORDER BY sync_run_id DESC LIMIT 1""",
            (connection_id, start_date.isoformat(), end_date.isoformat()),
        ).fetchone()
        if current is None:
            return
        stored = conn.execute(
            """SELECT mapping_id, provider_transaction_key_index, source_record_hash
               FROM provider_transaction_checkpoint_records WHERE checkpoint_id=?
               ORDER BY mapping_id, provider_transaction_key_index""",
            (current["id"],),
        ).fetchall()
        expected = {
            (item.mapping.id, item.provider_key_index, item.source_record_hash)
            for item in classified
        }
        actual = {
            (
                str(row["mapping_id"]),
                str(row["provider_transaction_key_index"]),
                str(row["source_record_hash"]),
            )
            for row in stored
        }
        if actual == expected:
            raise TransactionRefreshError("provider_transaction_replay")
        if not actual.issubset(expected):
            raise TransactionRefreshError("provider_transaction_changed")


def _window(start_date: date, end_date: date) -> None:
    if (
        not isinstance(start_date, date)
        or isinstance(start_date, datetime)
        or not isinstance(end_date, date)
        or isinstance(end_date, datetime)
        or start_date > end_date
        or end_date - start_date > TRANSACTION_MAX_RANGE
    ):
        raise ValueError("Schwab transaction date window is invalid")
