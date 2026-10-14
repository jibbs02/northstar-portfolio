"""Publish held-security and Bitcoin price observations as one explicit checkpoint.

The service never converts a missing, stale, or unusable observation into a number. A gap is
recorded as `unknown` with no value, the last valid observation for that security is carried
forward with its original provenance and time, and the checkpoint is marked incomplete so
dependent valuation and Plan inputs fail closed rather than consuming invented data.

Observations are published to the provider-owned checkpoint tables, not into
`market_price_snapshots`. Dashboard and Plan therefore stay disconnected from live prices until
their separate acceptance gates pass.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from northstar.data.connections import ProviderConnectionRepository
from northstar.data.database import Database, utc_now
from northstar.data.portfolio import decrypted_json
from northstar.domain import DecimalKind, canonical_decimal
from northstar.integrations import (
    BITCOIN_SPOT_SOURCE,
    BitcoinSpotObservation,
    SchwabOAuthError,
    SchwabQuote,
    SpotPriceError,
)
from northstar.integrations.schwab import MAX_QUOTE_SYMBOLS
from northstar.services.schwab_snapshots import (
    SchwabSnapshotService,
    SnapshotMappingSet,
    SnapshotRefreshError,
)

PRICE_PROVENANCE = "schwab_synthetic_prices_v1"
SCHWAB_HTTP_PRICE_PROVENANCE = "schwab_http_prices_v1"
PRICE_PROVENANCES = frozenset({PRICE_PROVENANCE, SCHWAB_HTTP_PRICE_PROVENANCE})
SCHWAB_QUOTE_SOURCE = "schwab_quotes"
PRICE_LOAD_TIMEOUT = timedelta(seconds=30)
DEFAULT_PRICE_STALE_AFTER_HOURS = 48
MAX_PRICE_SECURITIES = MAX_QUOTE_SYMBOLS
MAX_MANUAL_SOURCE_LENGTH = 64
# Local instrument types Schwab can quote, each mapped to the documented provider asset family it
# resolves to. The quotable set is derived from this mapping so it cannot drift away from
# `SCHWAB_QUOTE_ASSET_MAIN_TYPES`: requesting a symbol whose family the transport does not support
# fails the whole batch, not just that symbol, so one unquotable holding would strand every price.
# Cash is never quoted and Bitcoin uses its own documented source. `bond` and `other` have no
# supported quote family and remain manual-only.
QUOTABLE_ASSET_MAIN_TYPES = {
    "equity": "EQUITY",
    "etf": "EQUITY",
    "mutual_fund": "MUTUAL_FUND",
}
QUOTABLE_INSTRUMENT_TYPES = frozenset(QUOTABLE_ASSET_MAIN_TYPES)
BITCOIN_INSTRUMENT_TYPE = "bitcoin"


class PriceRefreshError(ValueError):
    """An allowlisted, privacy-safe price refresh failure."""


@dataclass(frozen=True)
class HeldSecurity:
    security_id: str
    symbol: str = field(repr=False)
    instrument_type: str


@dataclass(frozen=True)
class ManualPriceObservation:
    """An explicit manual fallback price with its own provenance.

    Manual values are accepted only for securities the live sources could not price. They never
    silently override a live observation.
    """

    symbol: str = field(repr=False)
    price: Decimal = field(repr=False)
    observed_at_utc: datetime = field(repr=False)
    source: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.symbol, str)
            or not self.symbol
            or len(self.symbol) > 32
            or self.symbol != self.symbol.strip().upper()
            or not self.symbol.isascii()
        ):
            raise ValueError("Manual price symbol is malformed")
        if (
            not isinstance(self.source, str)
            or not self.source.startswith("manual")
            or len(self.source) > MAX_MANUAL_SOURCE_LENGTH
            or self.source != self.source.strip()
            or not self.source.isascii()
            or any(character.isspace() for character in self.source)
        ):
            raise ValueError("Manual price source is malformed")
        if not isinstance(self.price, Decimal) or self.price <= 0:
            raise ValueError("Manual price is malformed")
        canonical_decimal(self.price, DecimalKind.PRICE, allow_negative=False)
        if self.observed_at_utc.tzinfo is None:
            raise ValueError("Manual price observation time is malformed")
        # Normalized so a caller-supplied offset cannot reach the stored `observed_at_utc` column,
        # whose text ordering decides which observation is carried forward.
        object.__setattr__(self, "observed_at_utc", self.observed_at_utc.astimezone(UTC))


@dataclass(frozen=True)
class PriceObservationView:
    security_id: str
    source: str
    price_status: str
    freshness: str
    price: Decimal | None = field(repr=False, default=None)
    previous_close: Decimal | None = field(repr=False, default=None)
    observed_at_utc: datetime | None = field(repr=False, default=None)
    market_session: str | None = None
    quote_context: str | None = None


@dataclass(frozen=True)
class PricePublication:
    id: str
    published_at: datetime
    provenance: str
    completeness: str
    price_stale_after_hours: int
    quote_source_status: str
    quote_source_error_code: str | None
    bitcoin_source_status: str
    bitcoin_source_error_code: str | None
    held_security_count: int
    observed_count: int
    carried_forward_count: int
    manual_count: int
    unknown_count: int
    observations: tuple[PriceObservationView, ...]

    @property
    def stale_count(self) -> int:
        return sum(1 for item in self.observations if item.freshness == "stale")

    @property
    def usable_for_valuation(self) -> bool:
        """Whether every held security has a complete, fresh observation.

        Plan and any valuation consumer must treat `False` as fail-closed rather than
        substituting or ignoring the missing inputs.
        """
        return self.completeness == "complete" and all(
            item.freshness == "fresh" for item in self.observations
        )


@dataclass(frozen=True)
class _ResolvedObservation:
    security: HeldSecurity
    source: str
    price_status: str
    price: Decimal | None
    previous_close: Decimal | None
    observed_at_utc: datetime | None
    market_session: str | None
    quote_context: str | None


class SchwabPriceService:
    """Resolve held securities, read bounded prices, and publish one atomic checkpoint."""

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
        load_quotes: Callable[[tuple[str, ...]], Awaitable[tuple[SchwabQuote, ...]]],
        load_bitcoin_spot: Callable[[], Awaitable[BitcoinSpotObservation]] | None = None,
        manual_observations: tuple[ManualPriceObservation, ...] = (),
        provenance: str = PRICE_PROVENANCE,
        mapping_set: SnapshotMappingSet | None = None,
    ) -> PricePublication:
        if provenance not in PRICE_PROVENANCES:
            raise ValueError("Price provenance is not allowed")
        lock = self._locks.setdefault(connection_id, asyncio.Lock())
        if lock.locked():
            raise PriceRefreshError("price_refresh_in_progress")
        async with lock:
            # Checked before a sync run exists so a disconnected provider produces an
            # allowlisted category instead of a repository error and a stranded running row.
            if self.connections.get(connection_id).status != "connected":
                raise PriceRefreshError("price_connection_changed")
            sync_run = self.connections.start_sync(connection_id, actor=actor)
            observed_count = 0
            try:
                pinned = mapping_set or self._mapping_guard.capture_mapping_set(connection_id)
                self._mapping_guard._validate_mapping_set(connection_id, pinned)
                held = self.held_securities(connection_id)
                if not held:
                    raise PriceRefreshError("price_no_held_securities")
                if len(held) > MAX_PRICE_SECURITIES:
                    raise PriceRefreshError("price_held_security_cap_exceeded")
                loaded = await self._load_all(held, load_quotes, load_bitcoin_spot)
                observed_count = len(loaded.quotes) + (1 if loaded.spot is not None else 0)
                publication = self._publish(
                    connection_id,
                    sync_run,
                    pinned,
                    held,
                    loaded,
                    manual_observations,
                    provenance=provenance,
                )
            except PriceRefreshError as exc:
                self._fail(sync_run, observed_count, str(exc))
                raise
            except SnapshotRefreshError as exc:
                code = str(exc).replace("snapshot_", "price_", 1)
                self._fail(sync_run, observed_count, code)
                raise PriceRefreshError(code) from None
            except TimeoutError:
                # The whole load exceeded its bound, so nothing is published and the prior
                # checkpoint stands.
                self._fail(sync_run, observed_count, "price_load_timeout")
                raise PriceRefreshError("price_load_timeout") from None
            except asyncio.CancelledError:
                self._fail(sync_run, observed_count, "price_refresh_cancelled")
                raise
            except Exception:
                self._fail(sync_run, observed_count, "price_refresh_failed")
                raise PriceRefreshError("price_refresh_failed") from None
            return publication

    def _fail(self, sync_run: int, records_seen: int, error_code: str) -> None:
        self.connections.finish_sync(
            sync_run,
            succeeded=False,
            records_seen=records_seen,
            records_written=0,
            error_code=error_code,
        )

    def held_securities(self, connection_id: str) -> tuple[HeldSecurity, ...]:
        """Return known securities actually held, from provider and manual positions alike.

        Quotes are requested only for these. Discovery of unheld symbols is deliberately
        impossible, and CSV/manual holdings are never excluded.
        """
        with closing(self.database.connect()) as conn:
            snapshot = conn.execute(
                """SELECT id FROM provider_portfolio_snapshots
                   WHERE connection_id=? AND status='published'
                   ORDER BY sync_run_id DESC LIMIT 1""",
                (connection_id,),
            ).fetchone()
            candidates: dict[str, Decimal] = {}
            if snapshot is not None:
                for row in conn.execute(
                    """SELECT security_id, quantity FROM provider_position_snapshots
                       WHERE snapshot_id=?""",
                    (str(snapshot["id"]),),
                ).fetchall():
                    _accumulate(candidates, str(row["security_id"]), row["quantity"])
            for row in conn.execute(
                """WITH ranked AS (
                       SELECT p.security_id, p.quantity, ROW_NUMBER() OVER (
                           PARTITION BY p.account_id, p.security_id
                           ORDER BY p.source_date DESC, p.id DESC
                       ) AS rank
                       FROM position_snapshots p
                       JOIN accounts a ON a.id=p.account_id AND a.status='active'
                       WHERE p.reversed_at IS NULL
                   )
                   SELECT security_id, quantity FROM ranked WHERE rank=1"""
            ).fetchall():
                _accumulate(candidates, str(row["security_id"]), row["quantity"])
            held: list[HeldSecurity] = []
            for security_id, quantity in candidates.items():
                if quantity <= 0:
                    continue
                row = conn.execute(
                    """SELECT id, instrument_type, encrypted_symbol FROM securities
                       WHERE id=?""",
                    (security_id,),
                ).fetchone()
                if row is None or row["encrypted_symbol"] is None:
                    continue
                instrument_type = str(row["instrument_type"])
                if instrument_type == "cash":
                    continue
                symbol = str(
                    decrypted_json(
                        self.database, "securities", security_id, "symbol", row["encrypted_symbol"]
                    )
                ).upper()
                held.append(
                    HeldSecurity(
                        security_id=security_id, symbol=symbol, instrument_type=instrument_type
                    )
                )
        # Sorted by symbol so the outbound quote batch is deterministic regardless of local ids.
        return tuple(sorted(held, key=lambda item: (item.symbol, item.security_id)))

    async def _load_all(
        self,
        held: tuple[HeldSecurity, ...],
        load_quotes: Callable[[tuple[str, ...]], Awaitable[tuple[SchwabQuote, ...]]],
        load_bitcoin_spot: Callable[[], Awaitable[BitcoinSpotObservation]] | None,
    ) -> _LoadResult:
        symbols = tuple(
            item.symbol for item in held if item.instrument_type in QUOTABLE_INSTRUMENT_TYPES
        )
        if len(set(symbols)) != len(symbols):
            raise PriceRefreshError("price_duplicate_held_symbol")
        wants_bitcoin = any(
            item.instrument_type == BITCOIN_INSTRUMENT_TYPE for item in held
        )
        async with asyncio.timeout(PRICE_LOAD_TIMEOUT.total_seconds()):
            quotes: dict[str, SchwabQuote] = {}
            quote_status = "not_required"
            quote_error: str | None = None
            if symbols:
                try:
                    result = await load_quotes(symbols)
                    quotes = _quotes_by_symbol(result, symbols)
                    quote_status = "succeeded"
                except SchwabOAuthError as exc:
                    # A source outage degrades the checkpoint; it never fabricates a price.
                    quote_status, quote_error = "failed", exc.code
                except TimeoutError:
                    quote_status, quote_error = "failed", "provider_timeout"
            spot: BitcoinSpotObservation | None = None
            spot_status = "not_required"
            spot_error: str | None = None
            if wants_bitcoin:
                if load_bitcoin_spot is None:
                    spot_status, spot_error = "failed", "spot_source_not_configured"
                else:
                    try:
                        spot = await load_bitcoin_spot()
                        if not isinstance(spot, BitcoinSpotObservation):
                            raise PriceRefreshError("price_spot_observation_invalid")
                        spot_status = "succeeded"
                    except SpotPriceError as exc:
                        spot_status, spot_error = "failed", exc.code
                    except TimeoutError:
                        spot_status, spot_error = "failed", "spot_timeout"
        return _LoadResult(
            quotes=quotes,
            quote_status=quote_status,
            quote_error=quote_error,
            spot=spot,
            spot_status=spot_status,
            spot_error=spot_error,
        )

    def current(
        self, connection_id: str, *, now: datetime | None = None
    ) -> PricePublication | None:
        now = now or datetime.now(UTC)
        with closing(self.database.connect()) as conn:
            row = conn.execute(
                """SELECT * FROM provider_price_checkpoints WHERE connection_id=?
                   AND status='published' ORDER BY sync_run_id DESC LIMIT 1""",
                (connection_id,),
            ).fetchone()
            if row is None:
                return None
            observations = conn.execute(
                """SELECT * FROM provider_price_observations WHERE checkpoint_id=?
                   ORDER BY security_id""",
                (str(row["id"]),),
            ).fetchall()
        stale_after = int(row["price_stale_after_hours"])
        return PricePublication(
            id=str(row["id"]),
            published_at=datetime.fromisoformat(row["published_at"]),
            provenance=str(row["provenance"]),
            completeness=str(row["completeness"]),
            price_stale_after_hours=stale_after,
            quote_source_status=str(row["quote_source_status"]),
            quote_source_error_code=(
                str(row["quote_source_error_code"])
                if row["quote_source_error_code"] is not None
                else None
            ),
            bitcoin_source_status=str(row["bitcoin_source_status"]),
            bitcoin_source_error_code=(
                str(row["bitcoin_source_error_code"])
                if row["bitcoin_source_error_code"] is not None
                else None
            ),
            held_security_count=int(row["held_security_count"]),
            observed_count=int(row["observed_count"]),
            carried_forward_count=int(row["carried_forward_count"]),
            manual_count=int(row["manual_count"]),
            unknown_count=int(row["unknown_count"]),
            observations=tuple(
                PriceObservationView(
                    security_id=str(item["security_id"]),
                    source=str(item["source"]),
                    price_status=str(item["price_status"]),
                    # Freshness is recomputed on every read so an aging observation cannot keep
                    # presenting itself as fresh.
                    freshness=_freshness(item["observed_at_utc"], now, stale_after),
                    price=Decimal(item["price"]) if item["price"] is not None else None,
                    previous_close=(
                        Decimal(item["previous_close"])
                        if item["previous_close"] is not None
                        else None
                    ),
                    observed_at_utc=(
                        datetime.fromisoformat(item["observed_at_utc"])
                        if item["observed_at_utc"] is not None
                        else None
                    ),
                    market_session=(
                        str(item["market_session"])
                        if item["market_session"] is not None
                        else None
                    ),
                    quote_context=(
                        str(item["quote_context"]) if item["quote_context"] is not None else None
                    ),
                )
                for item in observations
            ),
        )

    def _publish(
        self,
        connection_id: str,
        sync_run: int,
        mapping_set: SnapshotMappingSet,
        held: tuple[HeldSecurity, ...],
        loaded: _LoadResult,
        manual_observations: tuple[ManualPriceObservation, ...],
        *,
        provenance: str,
    ) -> PricePublication:
        stale_after = self._price_stale_after_hours()
        checkpoint_id = str(uuid.uuid4())
        with self.database.transaction() as conn:
            self._mapping_guard._validate_mapping_set(connection_id, mapping_set, conn=conn)
            manual_by_security = _manual_by_security(manual_observations, held)
            resolved: list[_ResolvedObservation] = []
            for security in held:
                live = _live_observation(security, loaded)
                manual = manual_by_security.get(security.security_id)
                if live is not None:
                    if manual is not None:
                        # A manual value that contradicts a successful live read is a conflict,
                        # never a silent override.
                        raise PriceRefreshError("price_conflicting_observation")
                    resolved.append(live)
                    continue
                if manual is not None:
                    resolved.append(
                        _ResolvedObservation(
                            security=security,
                            source=manual.source,
                            price_status="manual",
                            price=manual.price,
                            previous_close=None,
                            observed_at_utc=manual.observed_at_utc,
                            market_session=None,
                            quote_context=None,
                        )
                    )
                    continue
                resolved.append(self._carry_forward(conn, security))
            counts = {
                status: sum(1 for item in resolved if item.price_status == status)
                for status in ("observed", "carried_forward", "manual", "unknown")
            }
            complete = (
                counts["carried_forward"] == 0
                and counts["unknown"] == 0
                and loaded.quote_status != "failed"
                and loaded.spot_status != "failed"
            )
            now = utc_now()
            conn.execute(
                """INSERT INTO provider_price_checkpoints(
                       id, connection_id, sync_run_id, status, published_at, provenance,
                       completeness, price_stale_after_hours, quote_source_status,
                       quote_source_error_code, bitcoin_source_status, bitcoin_source_error_code,
                       held_security_count, observed_count, carried_forward_count, manual_count,
                       unknown_count, audit_metadata_json
                   ) VALUES (?, ?, ?, 'published', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkpoint_id,
                    connection_id,
                    sync_run,
                    now,
                    provenance,
                    "complete" if complete else "incomplete",
                    stale_after,
                    loaded.quote_status,
                    loaded.quote_error,
                    loaded.spot_status,
                    loaded.spot_error,
                    len(held),
                    counts["observed"],
                    counts["carried_forward"],
                    counts["manual"],
                    counts["unknown"],
                    json.dumps(
                        {
                            "held_security_scope": "provider_and_manual_positions",
                            "raw_response_stored": False,
                            "zero_price_substituted": False,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
            published_at = datetime.fromisoformat(now)
            conn.executemany(
                """INSERT INTO provider_price_observations(
                       checkpoint_id, security_id, source, price_status, price, previous_close,
                       observed_at_utc, market_session, quote_context, freshness
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        checkpoint_id,
                        item.security.security_id,
                        item.source,
                        item.price_status,
                        _price_text(item.price),
                        _price_text(item.previous_close),
                        _observed_text(item.observed_at_utc),
                        item.market_session,
                        item.quote_context,
                        _freshness(
                            _observed_text(item.observed_at_utc),
                            published_at,
                            stale_after,
                        ),
                    )
                    for item in resolved
                ],
            )
            conn.execute(
                """UPDATE sync_runs SET status='succeeded', records_seen=?, records_written=?,
                   error_code=NULL, finished_at=? WHERE id=? AND status='running'""",
                (len(held), len(resolved), now, sync_run),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise PriceRefreshError("price_sync_state_changed")
            if complete:
                conn.execute(
                    """UPDATE provider_connections SET last_successful_sync_at=?, updated_at=?
                       WHERE id=?""",
                    (now, now, connection_id),
                )
            conn.execute(
                """INSERT INTO audit_events(
                       event_type, actor_hash, subject_hash, metadata_json, created_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    "schwab_prices_published",
                    self.database.vault.blind_index("actor", "price-service"),
                    self.database.vault.blind_index("subject", connection_id),
                    json.dumps(
                        {
                            "carried_forward": counts["carried_forward"],
                            "completeness": "complete" if complete else "incomplete",
                            "held_securities": len(held),
                            "manual": counts["manual"],
                            "observed": counts["observed"],
                            "raw_response_stored": False,
                            "unknown": counts["unknown"],
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    now,
                ),
            )
        return self.current(connection_id) or (_ for _ in ()).throw(
            AssertionError("published price checkpoint missing")
        )

    def _carry_forward(self, conn: Any, security: HeldSecurity) -> _ResolvedObservation:
        """Reuse the newest valid observation for this security, across all checkpoints.

        Searching every published checkpoint — not only the newest — means a total source outage
        still preserves the last valid observation instead of degrading it to unknown.
        """
        row = conn.execute(
            """SELECT observation.source, observation.price, observation.previous_close,
                      observation.observed_at_utc, observation.market_session,
                      observation.quote_context
               FROM provider_price_observations AS observation
               JOIN provider_price_checkpoints AS checkpoint
                 ON checkpoint.id=observation.checkpoint_id
               WHERE observation.security_id=? AND observation.price_status<>'unknown'
                 AND checkpoint.status='published'
               ORDER BY observation.observed_at_utc DESC, checkpoint.sync_run_id DESC
               LIMIT 1""",
            (security.security_id,),
        ).fetchone()
        if row is None:
            return _ResolvedObservation(
                security=security,
                source="none",
                price_status="unknown",
                price=None,
                previous_close=None,
                observed_at_utc=None,
                market_session=None,
                quote_context=None,
            )
        return _ResolvedObservation(
            security=security,
            source=str(row["source"]),
            price_status="carried_forward",
            price=Decimal(row["price"]),
            previous_close=(
                Decimal(row["previous_close"]) if row["previous_close"] is not None else None
            ),
            observed_at_utc=datetime.fromisoformat(row["observed_at_utc"]),
            market_session=(
                str(row["market_session"]) if row["market_session"] is not None else None
            ),
            quote_context=(
                str(row["quote_context"]) if row["quote_context"] is not None else None
            ),
        )

    def _price_stale_after_hours(self) -> int:
        with closing(self.database.connect()) as conn:
            row = conn.execute(
                "SELECT price_stale_after_hours FROM portfolio_rules WHERE id=1"
            ).fetchone()
        if row is None:
            return DEFAULT_PRICE_STALE_AFTER_HOURS
        return int(row["price_stale_after_hours"])


@dataclass(frozen=True)
class _LoadResult:
    quotes: dict[str, SchwabQuote]
    quote_status: str
    quote_error: str | None
    spot: BitcoinSpotObservation | None
    spot_status: str
    spot_error: str | None


def _accumulate(candidates: dict[str, Decimal], security_id: str, quantity: Any) -> None:
    try:
        parsed = Decimal(str(quantity))
    except (InvalidOperation, ValueError):
        raise PriceRefreshError("price_held_position_invalid") from None
    candidates[security_id] = candidates.get(security_id, Decimal()) + parsed


def _quotes_by_symbol(
    result: Any, symbols: tuple[str, ...]
) -> dict[str, SchwabQuote]:
    if not isinstance(result, tuple) or any(
        not isinstance(item, SchwabQuote) for item in result
    ):
        raise PriceRefreshError("price_quote_response_invalid")
    quotes = {item.symbol: item for item in result}
    if len(quotes) != len(result) or frozenset(quotes) != frozenset(symbols):
        # The transport already guarantees exact coverage; this re-checks it at the boundary that
        # decides valuation inputs.
        raise PriceRefreshError("price_symbol_coverage_conflict")
    return quotes


def _manual_by_security(
    manual_observations: tuple[ManualPriceObservation, ...],
    held: tuple[HeldSecurity, ...],
) -> dict[str, ManualPriceObservation]:
    by_symbol = {item.symbol: item.security_id for item in held}
    resolved: dict[str, ManualPriceObservation] = {}
    for observation in manual_observations:
        if not isinstance(observation, ManualPriceObservation):
            raise PriceRefreshError("price_manual_observation_invalid")
        security_id = by_symbol.get(observation.symbol)
        if security_id is None:
            raise PriceRefreshError("price_manual_unknown_security")
        if security_id in resolved:
            raise PriceRefreshError("price_conflicting_observation")
        resolved[security_id] = observation
    return resolved


def _live_observation(
    security: HeldSecurity, loaded: _LoadResult
) -> _ResolvedObservation | None:
    if security.instrument_type == BITCOIN_INSTRUMENT_TYPE:
        if loaded.spot is None:
            return None
        return _ResolvedObservation(
            security=security,
            source=BITCOIN_SPOT_SOURCE,
            price_status="observed",
            price=loaded.spot.price,
            previous_close=None,
            observed_at_utc=loaded.spot.observed_at_utc,
            market_session=None,
            quote_context=None,
        )
    quote = loaded.quotes.get(security.symbol)
    if quote is None:
        return None
    return _ResolvedObservation(
        security=security,
        source=SCHWAB_QUOTE_SOURCE,
        price_status="observed",
        price=quote.price,
        previous_close=quote.previous_close,
        observed_at_utc=quote.observed_at_utc,
        market_session=quote.market_session,
        quote_context=quote.quote_context,
    )


def _price_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return canonical_decimal(value, DecimalKind.PRICE, allow_negative=False)


def _observed_text(value: datetime | None) -> str | None:
    """Store observation times as genuine UTC.

    `_carry_forward` orders by this column as text, so a stored offset other than `+00:00` would
    let an older observation sort ahead of a newer one.
    """
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _freshness(observed_at: Any, now: datetime, stale_after_hours: int) -> str:
    if observed_at is None:
        return "unknown"
    observed = datetime.fromisoformat(str(observed_at))
    elapsed = now - observed
    return "stale" if elapsed.total_seconds() > stale_after_hours * 3600 else "fresh"
