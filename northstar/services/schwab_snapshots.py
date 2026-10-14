from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from northstar.data.connections import ProviderConnectionRepository
from northstar.data.database import Database, utc_now
from northstar.domain import DecimalKind, canonical_decimal
from northstar.integrations import SchwabAccountSnapshot, SchwabOAuthError

SNAPSHOT_PROVENANCE = "schwab_synthetic_read_only_v1"
SCHWAB_HTTP_SNAPSHOT_PROVENANCE = "schwab_http_read_only_v1"
SNAPSHOT_PROVENANCES = frozenset({SNAPSHOT_PROVENANCE, SCHWAB_HTTP_SNAPSHOT_PROVENANCE})
SNAPSHOT_FRESH_FOR = timedelta(hours=24)
SNAPSHOT_LOAD_TIMEOUT = timedelta(seconds=30)


class SnapshotRefreshError(ValueError):
    """An allowlisted, privacy-safe refresh failure."""


@dataclass(frozen=True)
class SnapshotPublication:
    id: str
    observed_at_utc: datetime
    published_at: datetime
    freshness: str
    provenance: str
    reconciliation_status: str
    mapped_account_count: int
    position_count: int


@dataclass(frozen=True)
class SnapshotMappingReference:
    id: str
    account_id: str
    account_type: str
    external_account_hash_index: str = field(repr=False)
    mapping_updated_at: str
    account_updated_at: str
    provider_account_hash: str = field(repr=False)


@dataclass(frozen=True)
class SnapshotMappingSet:
    connection_id: str
    connection_updated_at: str
    mappings: tuple[SnapshotMappingReference, ...]

    @property
    def provider_account_hashes(self) -> frozenset[str]:
        return frozenset(item.provider_account_hash for item in self.mappings)


class SchwabSnapshotService:
    """Validate all mapped synthetic reads before one immutable snapshot is published.

    This service deliberately has no HTTP client. Its loader is injected by development tests;
    a production request cannot be made from this slice.
    """

    def __init__(self, database: Database, connections: ProviderConnectionRepository) -> None:
        self.database = database
        self.connections = connections
        self._locks: dict[str, asyncio.Lock] = {}

    async def refresh(
        self,
        *,
        connection_id: str,
        actor: str,
        load_snapshot: Callable[[str], Awaitable[SchwabAccountSnapshot]],
        provenance: str = SNAPSHOT_PROVENANCE,
        mapping_set: SnapshotMappingSet | None = None,
    ) -> SnapshotPublication:
        if provenance not in SNAPSHOT_PROVENANCES:
            raise ValueError("Snapshot provenance is not allowed")
        lock = self._locks.setdefault(connection_id, asyncio.Lock())
        if lock.locked():
            raise SnapshotRefreshError("snapshot_refresh_in_progress")
        async with lock:
            sync_run = self.connections.start_sync(connection_id, actor=actor)
            try:
                pinned = mapping_set or self.capture_mapping_set(connection_id)
                self._validate_mapping_set(connection_id, pinned)
                mappings = pinned.mappings
                if not mappings:
                    raise SnapshotRefreshError("snapshot_no_mapped_accounts")
                snapshots = await self._load_all(load_snapshot, mappings)
                publication = self._publish(
                    connection_id, sync_run, pinned, snapshots, provenance=provenance
                )
            except SnapshotRefreshError as exc:
                self.connections.finish_sync(
                    sync_run, succeeded=False, records_seen=0, records_written=0, error_code=str(exc)
                )
                raise
            except SchwabOAuthError as exc:
                self.connections.finish_sync(
                    sync_run, succeeded=False, records_seen=0, records_written=0, error_code=exc.code
                )
                raise SnapshotRefreshError(exc.code) from None
            except TimeoutError:
                self.connections.finish_sync(
                    sync_run,
                    succeeded=False,
                    records_seen=0,
                    records_written=0,
                    error_code="provider_timeout",
                )
                raise SnapshotRefreshError("provider_timeout") from None
            except asyncio.CancelledError:
                self.connections.finish_sync(
                    sync_run,
                    succeeded=False,
                    records_seen=0,
                    records_written=0,
                    error_code="refresh_cancelled",
                )
                raise
            except Exception:
                self.connections.finish_sync(
                    sync_run,
                    succeeded=False,
                    records_seen=0,
                    records_written=0,
                    error_code="provider_refresh_failed",
                )
                raise SnapshotRefreshError("provider_refresh_failed") from None
            return publication

    async def _load_all(
        self,
        loader: Callable[[str], Awaitable[SchwabAccountSnapshot]],
        mappings: tuple[SnapshotMappingReference, ...],
    ) -> tuple[SchwabAccountSnapshot, ...]:
        tasks = [
            asyncio.create_task(self._load_one(loader, reference))
            for reference in mappings
        ]
        try:
            async with asyncio.timeout(SNAPSHOT_LOAD_TIMEOUT.total_seconds()):
                return tuple(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _load_one(
        self,
        loader: Callable[[str], Awaitable[SchwabAccountSnapshot]],
        reference: SnapshotMappingReference,
    ) -> SchwabAccountSnapshot:
        try:
            result = await loader(reference.provider_account_hash)
        except SchwabOAuthError:
            raise
        except TimeoutError:
            raise SchwabOAuthError("provider_timeout") from None
        if (
            not isinstance(result, SchwabAccountSnapshot)
            or result.provider_account_hash != reference.provider_account_hash
        ):
            raise SnapshotRefreshError("provider_validation_account_identity_mechanics")
        if result.account_type is None:
            result = replace(result, account_type=reference.account_type)
        return result

    def current(self, connection_id: str, *, now: datetime | None = None) -> SnapshotPublication | None:
        now = now or datetime.now(UTC)
        with closing(self.database.connect()) as conn:
            row = conn.execute(
                """SELECT * FROM provider_portfolio_snapshots WHERE connection_id=?
                   AND status='published' ORDER BY sync_run_id DESC LIMIT 1""",
                (connection_id,),
            ).fetchone()
        if row is None:
            return None
        observed = datetime.fromisoformat(row["observed_at_utc"])
        published = datetime.fromisoformat(row["published_at"])
        return SnapshotPublication(
            id=str(row["id"]),
            observed_at_utc=observed,
            published_at=published,
            freshness="fresh" if now - observed <= SNAPSHOT_FRESH_FOR else "stale",
            provenance=str(row["provenance"]),
            reconciliation_status=str(row["reconciliation_status"]),
            mapped_account_count=int(row["mapped_account_count"]),
            position_count=int(row["position_count"]),
        )

    def capture_mapping_set(self, connection_id: str) -> SnapshotMappingSet:
        connection = self.connections.get(connection_id)
        if connection.status != "connected":
            raise SnapshotRefreshError("snapshot_connection_changed")
        with closing(self.database.connect()) as conn:
            rows = self._mapping_rows(conn, connection_id)
        from northstar.data.portfolio import decrypted_json

        return SnapshotMappingSet(
            connection_id=connection_id,
            connection_updated_at=connection.updated_at,
            mappings=tuple(
                SnapshotMappingReference(
                    id=str(row["id"]),
                    account_id=str(row["account_id"]),
                    account_type=str(row["account_type"]),
                    external_account_hash_index=str(row["external_account_hash_index"]),
                    mapping_updated_at=str(row["mapping_updated_at"]),
                    account_updated_at=str(row["account_updated_at"]),
                    provider_account_hash=str(
                        decrypted_json(
                            self.database,
                            "provider_account_mappings",
                            row["id"],
                            "external_account_hash",
                            row["encrypted_external_account_hash"],
                        )
                    ),
                )
                for row in rows
            )
        )

    def _validate_mapping_set(
        self,
        connection_id: str,
        mapping_set: SnapshotMappingSet,
        *,
        conn: Any | None = None,
    ) -> None:
        if mapping_set.connection_id != connection_id:
            raise SnapshotRefreshError("snapshot_mapping_changed")
        if conn is None:
            with closing(self.database.connect()) as connection:
                self._validate_mapping_set(connection_id, mapping_set, conn=connection)
            return
        current_connection = conn.execute(
            "SELECT status, updated_at FROM provider_connections WHERE id=?", (connection_id,)
        ).fetchone()
        if (
            current_connection is None
            or current_connection["status"] != "connected"
            or str(current_connection["updated_at"]) != mapping_set.connection_updated_at
        ):
            raise SnapshotRefreshError("snapshot_connection_changed")
        rows = self._mapping_rows(conn, connection_id)
        current = tuple(self._mapping_signature(row) for row in rows)
        expected = tuple(self._reference_signature(item) for item in mapping_set.mappings)
        if current != expected:
            raise SnapshotRefreshError("snapshot_mapping_changed")

    @staticmethod
    def _mapping_rows(conn: Any, connection_id: str):
        return conn.execute(
            """SELECT mapping.id, mapping.account_id, accounts.account_type,
                      mapping.external_account_hash_index,
                      mapping.encrypted_external_account_hash,
                      mapping.updated_at AS mapping_updated_at,
                      accounts.updated_at AS account_updated_at
               FROM provider_account_mappings AS mapping
               JOIN accounts ON accounts.id=mapping.account_id
               WHERE mapping.connection_id=? AND mapping.status='active'
                 AND accounts.status='active' ORDER BY mapping.id""",
            (connection_id,),
        ).fetchall()

    @staticmethod
    def _mapping_signature(row: Any) -> tuple[str, ...]:
        return (
            str(row["id"]),
            str(row["account_id"]),
            str(row["account_type"]),
            str(row["external_account_hash_index"]),
            str(row["mapping_updated_at"]),
            str(row["account_updated_at"]),
        )

    @staticmethod
    def _reference_signature(reference: SnapshotMappingReference) -> tuple[str, ...]:
        return (
            reference.id,
            reference.account_id,
            reference.account_type,
            reference.external_account_hash_index,
            reference.mapping_updated_at,
            reference.account_updated_at,
        )

    def _publish(
        self,
        connection_id: str,
        sync_run: int,
        mapping_set: SnapshotMappingSet,
        snapshots: tuple[SchwabAccountSnapshot, ...],
        *,
        provenance: str,
    ) -> SnapshotPublication:
        mappings = mapping_set.mappings
        if len(snapshots) != len(mappings):
            raise SnapshotRefreshError("provider_partial_data")
        snapshot_id = str(uuid.uuid4())
        observed_times = {item.observed_at_utc for item in snapshots}
        if len(observed_times) != 1:
            raise SnapshotRefreshError("provider_partial_data")
        observed = observed_times.pop()
        balances: list[tuple[SnapshotMappingReference, SchwabAccountSnapshot, str, str]] = []
        positions: list[tuple[SnapshotMappingReference, str, str, str, str, str]] = []
        with self.database.transaction() as conn:
            self._validate_mapping_set(connection_id, mapping_set, conn=conn)
            for mapping, account in zip(mappings, snapshots, strict=True):
                if _account_type(account.account_type) != mapping.account_type:
                    raise SnapshotRefreshError("provider_account_identity_conflict")
                liquidation = _amount(account.liquidation_value)
                cash = _amount(account.cash_balance)
                total_positions = sum(
                    (Decimal(_amount(position.market_value)) for position in account.positions), Decimal()
                )
                if abs(Decimal(liquidation) - (Decimal(cash) + total_positions)) > Decimal("0.01"):
                    raise SnapshotRefreshError("provider_reconciliation_failed")
                balances.append((mapping, account, liquidation, cash))
                for position in account.positions:
                    symbol = position.symbol
                    if symbol is None:
                        raise SnapshotRefreshError("provider_unknown_security")
                    security = conn.execute(
                        "SELECT id, instrument_type FROM securities WHERE symbol_hash=?",
                        (self.database.vault.blind_index("security-symbol", symbol.upper()),),
                    ).fetchone()
                    if security is None:
                        raise SnapshotRefreshError("provider_unknown_security")
                    if not instrument_type_compatible(
                        position.instrument_type, str(security["instrument_type"])
                    ):
                        raise SnapshotRefreshError("provider_instrument_identity_conflict")
                    instrument_key = self.database.vault.blind_index(
                        "provider-instrument:schwab", position.provider_instrument_key
                    )
                    binding = conn.execute(
                        """SELECT security_id FROM provider_instrument_bindings
                           WHERE connection_id=? AND provider_instrument_key_index=?""",
                        (connection_id, instrument_key),
                    ).fetchone()
                    if binding is not None and str(binding["security_id"]) != str(security["id"]):
                        raise SnapshotRefreshError("provider_instrument_identity_conflict")
                    if binding is None:
                        conn.execute(
                            """INSERT INTO provider_instrument_bindings(
                               connection_id, provider_instrument_key_index, security_id,
                               created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?)""",
                            (connection_id, instrument_key, str(security["id"]), utc_now(), utc_now()),
                        )
                    positions.append(
                        (
                            mapping,
                            str(security["id"]),
                            instrument_key,
                            _quantity(position.quantity),
                            _amount(position.market_value),
                            _price(position.average_price),
                        )
                    )
            now = utc_now()
            metadata = json.dumps(
                {"lock_scope": "connection", "raw_response_stored": False, "validation": "complete"},
                separators=(",", ":"),
                sort_keys=True,
            )
            conn.execute(
                """INSERT INTO provider_portfolio_snapshots(
                       id, connection_id, sync_run_id, status, observed_at_utc, published_at,
                       provenance, reconciliation_status, mapped_account_count, position_count,
                       audit_metadata_json
                   ) VALUES (?, ?, ?, 'published', ?, ?, ?, 'matched', ?, ?, ?)""",
                (
                    snapshot_id,
                    connection_id,
                    sync_run,
                    observed.isoformat(),
                    now,
                    provenance,
                    len(mappings),
                    len(positions),
                    metadata,
                ),
            )
            conn.executemany(
                """INSERT INTO provider_balance_snapshots(
                       snapshot_id, mapping_id, account_id, liquidation_value, cash_balance,
                       reconciliation_status
                   )
                   VALUES (?, ?, ?, ?, ?, 'matched')""",
                [
                    (snapshot_id, item.id, item.account_id, liquidation, cash)
                    for item, _, liquidation, cash in balances
                ],
            )
            conn.executemany(
                """INSERT INTO provider_position_snapshots(
                       snapshot_id, mapping_id, account_id, security_id, provider_instrument_key_index,
                       quantity, market_value, average_price
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (snapshot_id, item.id, item.account_id, security, key, quantity, value, price)
                    for item, security, key, quantity, value, price in positions
                ],
            )
            record_count = len(mappings) + len(positions)
            conn.execute(
                """UPDATE sync_runs SET status='succeeded', records_seen=?, records_written=?,
                   error_code=NULL, finished_at=? WHERE id=? AND status='running'""",
                (record_count, record_count, now, sync_run),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise SnapshotRefreshError("snapshot_sync_state_changed")
            conn.execute(
                "UPDATE provider_connections SET last_successful_sync_at=?, updated_at=? WHERE id=?",
                (now, now, connection_id),
            )
            conn.execute(
                """INSERT INTO audit_events(event_type, actor_hash, subject_hash, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    "schwab_snapshot_published",
                    self.database.vault.blind_index("actor", "snapshot-service"),
                    self.database.vault.blind_index("subject", connection_id),
                    json.dumps(
                        {
                            "mapped_accounts": len(mappings),
                            "positions": len(positions),
                            "raw_response_stored": False,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    now,
                ),
            )
        return self.current(connection_id) or (_ for _ in ()).throw(
            AssertionError("published snapshot missing")
        )


def _amount(value: Decimal | None) -> str:
    if value is None:
        raise SnapshotRefreshError("provider_partial_data")
    return canonical_decimal(value, DecimalKind.AMOUNT, allow_negative=False)


def _quantity(value: Decimal) -> str:
    return canonical_decimal(value, DecimalKind.QUANTITY, allow_negative=False)


def _price(value: Decimal | None) -> str:
    if value is None:
        raise SnapshotRefreshError("provider_partial_data")
    return canonical_decimal(value, DecimalKind.PRICE, allow_negative=False)


def _account_type(value: str | None) -> str:
    if not isinstance(value, str):
        raise SnapshotRefreshError("provider_account_identity_conflict")
    normalized = value.strip().lower().replace("-", "_")
    if normalized not in {"roth_ira", "hsa", "taxable_brokerage"}:
        raise SnapshotRefreshError("provider_account_identity_conflict")
    return normalized


def instrument_type_compatible(provider_type: str, local_type: str) -> bool:
    compatible_local_types = {
        "equity": {"equity", "etf"},
        "etf": {"etf"},
        "mutual_fund": {"mutual_fund"},
        "bond": {"bond"},
        "cash": {"cash"},
    }
    return local_type in compatible_local_types.get(provider_type, set())
