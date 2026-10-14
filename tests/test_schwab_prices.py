from __future__ import annotations

import asyncio
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from northstar.config import Settings
from northstar.data import Database, PortfolioRepository, ProviderConnectionRepository
from northstar.data.database import LATEST_SCHEMA_VERSION, sqlite3
from northstar.data.portfolio import encrypted_json
from northstar.integrations import (
    BITCOIN_SPOT_SOURCE,
    BitcoinSpotObservation,
    SchwabOAuthError,
    SchwabQuote,
    SchwabTokenGrant,
    SpotPriceError,
)
from northstar.integrations.schwab import SCHWAB_QUOTE_ASSET_MAIN_TYPES
from northstar.services import (
    BackupService,
    ManualPriceObservation,
    PriceRefreshError,
    SchwabPriceService,
)
from northstar.services.schwab_prices import (
    QUOTABLE_ASSET_MAIN_TYPES,
    QUOTABLE_INSTRUMENT_TYPES,
    SCHWAB_QUOTE_SOURCE,
)

ACTOR = "synthetic-price-actor"


def connected_repository(
    settings: Settings,
) -> tuple[Database, PortfolioRepository, ProviderConnectionRepository, str]:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    database.initialize()
    portfolio = PortfolioRepository(database)
    connections = ProviderConnectionRepository(database)
    connection, state = connections.start_oauth("synthetic-price-session")
    attempt = connections.consume_oauth(state, "synthetic-price-session")
    now = datetime.now(UTC)
    synthetic_access = "-".join(("synthetic", "price", "access", "credential"))
    synthetic_refresh = "-".join(("synthetic", "price", "refresh", "credential"))
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


def add_security(
    database: Database, *, symbol: str, instrument_type: str = "etf", asset_class: str = "equity"
) -> str:
    security_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    with database.transaction() as conn:
        conn.execute(
            """INSERT INTO securities(
                   id, instrument_type, currency, encrypted_symbol, symbol_hash,
                   encrypted_name, key_version, created_at, updated_at, asset_class
               ) VALUES (?, ?, 'USD', ?, ?, ?, ?, ?, ?, ?)""",
            (
                security_id,
                instrument_type,
                encrypted_json(database, "securities", security_id, "symbol", symbol),
                database.vault.blind_index("security-symbol", symbol),
                encrypted_json(database, "securities", security_id, "name", f"Synthetic {symbol}"),
                database.vault.key_version,
                now,
                now,
                asset_class,
            ),
        )
    return security_id


def add_position(
    database: Database,
    *,
    account_id: str,
    security_id: str,
    quantity: str = "10",
    source_date: str = "2026-08-14",
    reversed_at: str | None = None,
) -> None:
    with database.transaction() as conn:
        conn.execute(
            """INSERT INTO position_snapshots(
                   id, account_id, security_id, as_of_utc, source_date, quantity,
                   market_value, market_value_status, cost_basis, cost_basis_status,
                   import_batch_id, reversed_at
               ) VALUES (?, ?, ?, ?, ?, ?, NULL, 'unknown', NULL, 'unknown', NULL, ?)""",
            (
                str(uuid.uuid4()),
                account_id,
                security_id,
                f"{source_date}T20:00:00+00:00",
                source_date,
                quantity,
                reversed_at,
            ),
        )


def set_price_staleness(database: Database, hours: int) -> None:
    with database.transaction() as conn:
        conn.execute(
            """INSERT INTO portfolio_rules(
                   id, bitcoin_target_percentage, bitcoin_caution_percentage,
                   bitcoin_review_percentage, rebalance_threshold_percentage_points,
                   price_stale_after_hours, position_stale_after_hours, updated_at
               ) VALUES (1, '10.000000', '15.000000', '20.000000', '5.000000', ?, 48, ?)
               ON CONFLICT(id) DO UPDATE SET price_stale_after_hours=excluded.price_stale_after_hours""",
            (hours, datetime.now(UTC).isoformat()),
        )


def quote(
    symbol: str,
    price: str = "191.25",
    *,
    observed: datetime | None = None,
    previous_close: str = "190.10",
    session: str = "normal",
    asset_main_type: str = "EQUITY",
    context: str = "NBBO",
) -> SchwabQuote:
    return SchwabQuote(
        symbol=symbol,
        asset_main_type=asset_main_type,
        price=Decimal(price),
        previous_close=Decimal(previous_close),
        observed_at_utc=observed or datetime.now(UTC) - timedelta(minutes=5),
        quote_context=context,
        market_session=session,
    )


def spot(price: str = "64125.37", *, observed: datetime | None = None) -> BitcoinSpotObservation:
    return BitcoinSpotObservation(
        source=BITCOIN_SPOT_SOURCE,
        price=Decimal(price),
        observed_at_utc=observed or datetime.now(UTC) - timedelta(minutes=2),
    )


def held_fixture(
    settings: Settings, *, with_bitcoin: bool = True
) -> tuple[Database, ProviderConnectionRepository, str, dict[str, str]]:
    database, portfolio, connections, connection_id = connected_repository(settings)
    account = portfolio.create_account(
        name="Synthetic price account",
        account_type="roth_ira",
        tax_treatment="tax_free",
    )
    connections.map_account(
        connection_id, account.id, external_account_hash="synthetic-price-provider-hash"
    )
    securities = {
        "VTI": add_security(database, symbol="VTI", instrument_type="etf"),
        "VTSAX": add_security(database, symbol="VTSAX", instrument_type="mutual_fund"),
    }
    if with_bitcoin:
        securities["BTC"] = add_security(
            database, symbol="BTC", instrument_type="bitcoin", asset_class="bitcoin"
        )
    for security_id in securities.values():
        add_position(database, account_id=account.id, security_id=security_id)
    return database, connections, connection_id, securities


def loader(*quotes: SchwabQuote):
    requested: list[tuple[str, ...]] = []

    async def load(symbols: tuple[str, ...]) -> tuple[SchwabQuote, ...]:
        requested.append(symbols)
        return tuple(item for item in quotes if item.symbol in symbols)

    load.requested = requested  # type: ignore[attr-defined]
    return load


def failing_loader(error: Exception):
    async def load(symbols: tuple[str, ...]) -> tuple[SchwabQuote, ...]:
        del symbols
        raise error

    return load


def spot_loader(observation: BitcoinSpotObservation | None = None, error: Exception | None = None):
    async def load() -> BitcoinSpotObservation:
        if error is not None:
            raise error
        assert observation is not None
        return observation

    return load


def test_price_checkpoint_publishes_complete_observations_with_provenance(
    settings: Settings,
) -> None:
    database, connections, connection_id, securities = held_fixture(settings)
    service = SchwabPriceService(database, connections)
    observed = datetime.now(UTC) - timedelta(minutes=5)
    load = loader(
        quote("VTI", "191.25", observed=observed),
        quote("VTSAX", "121.44", observed=observed, asset_main_type="MUTUAL_FUND",
              context="NFL", session="closed"),
    )

    publication = asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=load,
            load_bitcoin_spot=spot_loader(spot()),
        )
    )

    # Only held, quotable securities are requested, exactly once each, and Bitcoin never goes to
    # the Schwab quote endpoint.
    assert load.requested == [("VTI", "VTSAX")]  # type: ignore[attr-defined]
    assert publication.completeness == "complete"
    assert publication.usable_for_valuation is True
    assert publication.held_security_count == 3
    assert publication.observed_count == 3
    assert (publication.carried_forward_count, publication.manual_count) == (0, 0)
    assert publication.unknown_count == 0
    assert publication.quote_source_status == "succeeded"
    assert publication.bitcoin_source_status == "succeeded"
    assert (publication.quote_source_error_code, publication.bitcoin_source_error_code) == (
        None,
        None,
    )

    by_security = {item.security_id: item for item in publication.observations}
    equity = by_security[securities["VTI"]]
    assert equity.source == SCHWAB_QUOTE_SOURCE
    assert equity.price_status == "observed"
    assert equity.price == Decimal("191.25000000")
    assert equity.previous_close == Decimal("190.10000000")
    assert equity.market_session == "normal"
    assert equity.quote_context == "NBBO"
    assert equity.freshness == "fresh"
    fund = by_security[securities["VTSAX"]]
    assert (fund.market_session, fund.quote_context) == ("closed", "NFL")
    bitcoin = by_security[securities["BTC"]]
    assert bitcoin.source == BITCOIN_SPOT_SOURCE
    assert bitcoin.price == Decimal("64125.37000000")
    assert bitcoin.market_session is None

    rendered = repr(publication.observations)
    assert all(value not in rendered for value in ("191.25", "121.44", "64125.37"))


def test_held_securities_covers_provider_and_manual_positions_only(settings: Settings) -> None:
    database, portfolio, connections, connection_id = connected_repository(settings)
    account = portfolio.create_account(
        name="Synthetic held account", account_type="roth_ira", tax_treatment="tax_free"
    )
    connections.map_account(connection_id, account.id, external_account_hash="synthetic-hash")
    held_id = add_security(database, symbol="VTI")
    cash_id = add_security(database, symbol="CASH", instrument_type="cash", asset_class="cash")
    zero_id = add_security(database, symbol="ZERO")
    reversed_id = add_security(database, symbol="GONE")
    add_position(database, account_id=account.id, security_id=held_id, quantity="10")
    add_position(database, account_id=account.id, security_id=cash_id, quantity="500")
    add_position(database, account_id=account.id, security_id=zero_id, quantity="0")
    add_position(
        database,
        account_id=account.id,
        security_id=reversed_id,
        quantity="4",
        reversed_at=datetime.now(UTC).isoformat(),
    )
    service = SchwabPriceService(database, connections)

    held = service.held_securities(connection_id)

    assert {item.security_id for item in held} == {held_id}
    assert held[0].symbol == "VTI"


def test_held_securities_prefers_the_newest_local_position_per_security(
    settings: Settings,
) -> None:
    database, portfolio, connections, connection_id = connected_repository(settings)
    account = portfolio.create_account(
        name="Synthetic sold account", account_type="roth_ira", tax_treatment="tax_free"
    )
    connections.map_account(connection_id, account.id, external_account_hash="synthetic-hash")
    sold_id = add_security(database, symbol="SOLD")
    add_position(
        database, account_id=account.id, security_id=sold_id, quantity="10",
        source_date="2026-08-01",
    )
    add_position(
        database, account_id=account.id, security_id=sold_id, quantity="0",
        source_date="2026-08-14",
    )
    service = SchwabPriceService(database, connections)

    # A fully exited position is no longer held and must not be quoted.
    assert service.held_securities(connection_id) == ()


def test_a_source_outage_records_unknown_without_ever_writing_a_zero(
    settings: Settings,
) -> None:
    database, connections, connection_id, securities = held_fixture(
        settings, with_bitcoin=False
    )
    service = SchwabPriceService(database, connections)

    publication = asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=failing_loader(SchwabOAuthError("provider_unavailable")),
        )
    )

    assert publication.completeness == "incomplete"
    assert publication.usable_for_valuation is False
    assert publication.quote_source_status == "failed"
    assert publication.quote_source_error_code == "provider_unavailable"
    assert publication.unknown_count == 2
    assert publication.observed_count == 0
    for observation in publication.observations:
        assert observation.price_status == "unknown"
        assert observation.price is None
        assert observation.observed_at_utc is None
        assert observation.freshness == "unknown"
    with closing(database.connect()) as conn:
        stored = conn.execute("SELECT price FROM provider_price_observations").fetchall()
    assert [row["price"] for row in stored] == [None, None]
    assert securities


@pytest.mark.parametrize(
    "error",
    [
        SchwabOAuthError("provider_rate_limited"),
        SchwabOAuthError("provider_timeout"),
        SchwabOAuthError("provider_authorization_rejected"),
        SchwabOAuthError("provider_validation_quote_variant_malformed"),
        SchwabOAuthError("provider_quote_symbol_unavailable"),
        TimeoutError(),
    ],
)
def test_quote_source_failures_are_recorded_as_privacy_safe_categories(
    settings: Settings, error: Exception
) -> None:
    database, connections, connection_id, _ = held_fixture(settings, with_bitcoin=False)
    service = SchwabPriceService(database, connections)

    publication = asyncio.run(
        service.refresh(
            connection_id=connection_id, actor=ACTOR, load_quotes=failing_loader(error)
        )
    )

    expected = getattr(error, "code", "provider_timeout")
    assert publication.quote_source_error_code == expected
    assert publication.completeness == "incomplete"
    assert all(value not in expected for value in ("191.25", "VTI", "VTSAX"))


def test_an_outage_carries_the_last_valid_observation_forward(settings: Settings) -> None:
    database, connections, connection_id, securities = held_fixture(
        settings, with_bitcoin=False
    )
    service = SchwabPriceService(database, connections)
    observed = datetime.now(UTC) - timedelta(minutes=10)
    asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=loader(
                quote("VTI", "191.25", observed=observed),
                quote("VTSAX", "121.44", observed=observed),
            ),
        )
    )

    publication = asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=failing_loader(SchwabOAuthError("provider_unavailable")),
        )
    )

    assert publication.completeness == "incomplete"
    assert publication.usable_for_valuation is False
    assert publication.carried_forward_count == 2
    assert publication.unknown_count == 0
    carried = {item.security_id: item for item in publication.observations}
    equity = carried[securities["VTI"]]
    # The carried observation keeps its original value, provenance, and observation time.
    assert equity.price_status == "carried_forward"
    assert equity.price == Decimal("191.25000000")
    assert equity.source == SCHWAB_QUOTE_SOURCE
    assert equity.observed_at_utc == observed
    assert equity.freshness == "fresh"


def test_repeated_outages_keep_carrying_the_original_valid_observation(
    settings: Settings,
) -> None:
    database, connections, connection_id, securities = held_fixture(
        settings, with_bitcoin=False
    )
    service = SchwabPriceService(database, connections)
    observed = datetime.now(UTC) - timedelta(hours=2)
    asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=loader(
                quote("VTI", "191.25", observed=observed),
                quote("VTSAX", "121.44", observed=observed),
            ),
        )
    )
    for _ in range(3):
        publication = asyncio.run(
            service.refresh(
                connection_id=connection_id,
                actor=ACTOR,
                load_quotes=failing_loader(SchwabOAuthError("provider_unavailable")),
            )
        )

    carried = {item.security_id: item for item in publication.observations}
    assert publication.carried_forward_count == 2
    assert carried[securities["VTI"]].price == Decimal("191.25000000")
    assert carried[securities["VTI"]].observed_at_utc == observed


def test_a_stale_observation_is_marked_and_blocks_valuation(settings: Settings) -> None:
    database, connections, connection_id, _ = held_fixture(settings, with_bitcoin=False)
    set_price_staleness(database, 6)
    service = SchwabPriceService(database, connections)
    observed = datetime.now(UTC) - timedelta(hours=12)

    publication = asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=loader(
                quote("VTI", "191.25", observed=observed),
                quote("VTSAX", "121.44", observed=observed),
            ),
        )
    )

    assert publication.price_stale_after_hours == 6
    assert publication.completeness == "complete"
    assert publication.stale_count == 2
    # Every input is present, yet a stale price must still fail closed for valuation.
    assert publication.usable_for_valuation is False
    assert all(item.freshness == "stale" for item in publication.observations)


def test_freshness_is_recomputed_on_every_read(settings: Settings) -> None:
    database, connections, connection_id, _ = held_fixture(settings, with_bitcoin=False)
    set_price_staleness(database, 4)
    service = SchwabPriceService(database, connections)
    observed = datetime.now(UTC) - timedelta(minutes=1)
    asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=loader(
                quote("VTI", observed=observed), quote("VTSAX", observed=observed)
            ),
        )
    )

    fresh = service.current(connection_id)
    later = service.current(connection_id, now=datetime.now(UTC) + timedelta(hours=5))

    assert fresh is not None and later is not None
    assert all(item.freshness == "fresh" for item in fresh.observations)
    assert all(item.freshness == "stale" for item in later.observations)
    assert later.usable_for_valuation is False


def test_manual_fallback_prices_a_security_the_live_source_could_not(
    settings: Settings,
) -> None:
    database, connections, connection_id, securities = held_fixture(
        settings, with_bitcoin=False
    )
    service = SchwabPriceService(database, connections)
    observed = datetime.now(UTC) - timedelta(hours=1)

    publication = asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=failing_loader(SchwabOAuthError("provider_unavailable")),
            manual_observations=(
                ManualPriceObservation(
                    symbol="VTI",
                    price=Decimal("188.40"),
                    observed_at_utc=observed,
                    source="manual_statement",
                ),
            ),
        )
    )

    by_security = {item.security_id: item for item in publication.observations}
    manual = by_security[securities["VTI"]]
    assert manual.price_status == "manual"
    assert manual.source == "manual_statement"
    assert manual.price == Decimal("188.40000000")
    assert manual.observed_at_utc == observed
    assert manual.freshness == "fresh"
    assert publication.manual_count == 1
    assert publication.unknown_count == 1
    # A manual fallback documents the gap; it does not claim the run was complete.
    assert publication.completeness == "incomplete"


def test_a_manual_observation_never_silently_overrides_a_live_one(settings: Settings) -> None:
    database, connections, connection_id, _ = held_fixture(settings, with_bitcoin=False)
    service = SchwabPriceService(database, connections)
    asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=loader(quote("VTI"), quote("VTSAX")),
        )
    )
    first = service.current(connection_id)

    with pytest.raises(PriceRefreshError, match="price_conflicting_observation"):
        asyncio.run(
            service.refresh(
                connection_id=connection_id,
                actor=ACTOR,
                load_quotes=loader(quote("VTI"), quote("VTSAX")),
                manual_observations=(
                    ManualPriceObservation(
                        symbol="VTI",
                        price=Decimal("1.00"),
                        observed_at_utc=datetime.now(UTC),
                        source="manual_statement",
                    ),
                ),
            )
        )

    assert first is not None
    current = service.current(connection_id)
    assert current is not None and current.id == first.id


@pytest.mark.parametrize(
    ("observations", "expected"),
    [
        (
            (
                ManualPriceObservation(
                    symbol="NOPE",
                    price=Decimal("1"),
                    observed_at_utc=datetime(2026, 8, 16, tzinfo=UTC),
                    source="manual_statement",
                ),
            ),
            "price_manual_unknown_security",
        ),
        (
            (
                ManualPriceObservation(
                    symbol="VTI",
                    price=Decimal("1"),
                    observed_at_utc=datetime(2026, 8, 16, tzinfo=UTC),
                    source="manual_statement",
                ),
                ManualPriceObservation(
                    symbol="VTI",
                    price=Decimal("2"),
                    observed_at_utc=datetime(2026, 8, 16, tzinfo=UTC),
                    source="manual_broker_portal",
                ),
            ),
            "price_conflicting_observation",
        ),
    ],
)
def test_invalid_manual_observation_sets_are_rejected_without_publishing(
    settings: Settings, observations: tuple[ManualPriceObservation, ...], expected: str
) -> None:
    database, connections, connection_id, _ = held_fixture(settings, with_bitcoin=False)
    service = SchwabPriceService(database, connections)

    with pytest.raises(PriceRefreshError, match=expected):
        asyncio.run(
            service.refresh(
                connection_id=connection_id,
                actor=ACTOR,
                load_quotes=failing_loader(SchwabOAuthError("provider_unavailable")),
                manual_observations=observations,
            )
        )

    assert service.current(connection_id) is None


@pytest.mark.parametrize(
    ("symbol", "price", "source", "observed"),
    [
        ("vti", Decimal("1"), "manual_statement", datetime(2026, 8, 16, tzinfo=UTC)),
        ("", Decimal("1"), "manual_statement", datetime(2026, 8, 16, tzinfo=UTC)),
        ("VTI", Decimal("0"), "manual_statement", datetime(2026, 8, 16, tzinfo=UTC)),
        ("VTI", Decimal("-1"), "manual_statement", datetime(2026, 8, 16, tzinfo=UTC)),
        ("VTI", Decimal("1"), "schwab_quotes", datetime(2026, 8, 16, tzinfo=UTC)),
        ("VTI", Decimal("1"), "manual statement", datetime(2026, 8, 16, tzinfo=UTC)),
        ("VTI", Decimal("1"), "manual_" + "x" * 64, datetime(2026, 8, 16, tzinfo=UTC)),
        ("VTI", Decimal("1"), "manual_statement", datetime(2026, 8, 16)),
    ],
)
def test_manual_observations_require_explicit_bounded_provenance(
    symbol: str, price: Decimal, source: str, observed: datetime
) -> None:
    with pytest.raises(ValueError):
        ManualPriceObservation(
            symbol=symbol, price=price, observed_at_utc=observed, source=source
        )


def test_a_bitcoin_outage_degrades_only_the_bitcoin_input(settings: Settings) -> None:
    database, connections, connection_id, securities = held_fixture(settings)
    service = SchwabPriceService(database, connections)

    publication = asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=loader(quote("VTI"), quote("VTSAX")),
            load_bitcoin_spot=spot_loader(error=SpotPriceError("spot_unavailable")),
        )
    )

    assert publication.quote_source_status == "succeeded"
    assert publication.bitcoin_source_status == "failed"
    assert publication.bitcoin_source_error_code == "spot_unavailable"
    assert publication.observed_count == 2
    assert publication.unknown_count == 1
    assert publication.completeness == "incomplete"
    by_security = {item.security_id: item for item in publication.observations}
    assert by_security[securities["BTC"]].price is None
    assert by_security[securities["VTI"]].price == Decimal("191.25000000")


def test_holding_bitcoin_without_a_configured_source_fails_closed(settings: Settings) -> None:
    database, connections, connection_id, _ = held_fixture(settings)
    service = SchwabPriceService(database, connections)

    publication = asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=loader(quote("VTI"), quote("VTSAX")),
        )
    )

    assert publication.bitcoin_source_status == "failed"
    assert publication.bitcoin_source_error_code == "spot_source_not_configured"
    assert publication.completeness == "incomplete"


def test_the_quotable_set_never_drifts_from_the_transports_supported_families(
) -> None:
    """A local type the transport cannot price would fail the whole batch, not just its symbol."""
    assert set(QUOTABLE_ASSET_MAIN_TYPES) == QUOTABLE_INSTRUMENT_TYPES
    assert set(QUOTABLE_ASSET_MAIN_TYPES.values()) <= SCHWAB_QUOTE_ASSET_MAIN_TYPES


def test_an_unquotable_holding_never_strands_the_prices_of_the_rest(settings: Settings) -> None:
    """A held bond has no supported quote family, so it must stay out of the outbound batch.

    The transport rejects an unsupported asset family for the whole response, so including one
    unquotable symbol would discard every other security's valid price.
    """
    database, connections, connection_id, securities = held_fixture(
        settings, with_bitcoin=False
    )
    with closing(database.connect()) as conn:
        account = str(
            conn.execute(
                "SELECT account_id FROM provider_account_mappings WHERE connection_id=?",
                (connection_id,),
            ).fetchone()["account_id"]
        )
    securities["TBILL"] = add_security(
        database, symbol="TBILL", instrument_type="bond", asset_class="fixed_income"
    )
    add_position(database, account_id=account, security_id=securities["TBILL"])
    service = SchwabPriceService(database, connections)
    load = loader(quote("VTI"), quote("VTSAX"))

    publication = asyncio.run(
        service.refresh(connection_id=connection_id, actor=ACTOR, load_quotes=load)
    )

    assert load.requested == [("VTI", "VTSAX")]  # type: ignore[attr-defined]
    assert publication.quote_source_status == "succeeded"
    by_security = {item.security_id: item for item in publication.observations}
    # The bond is an explicit gap, and the quotable holdings keep their real observations.
    assert by_security[securities["TBILL"]].price_status == "unknown"
    assert by_security[securities["TBILL"]].price is None
    assert by_security[securities["VTI"]].price_status == "observed"
    assert by_security[securities["VTSAX"]].price_status == "observed"
    assert publication.observed_count == 2
    assert publication.unknown_count == 1
    assert publication.completeness == "incomplete"


def test_a_manual_observation_is_stored_and_carried_forward_as_utc(settings: Settings) -> None:
    """`observed_at_utc` is ordered as text, so a caller offset must not survive to storage."""
    database, connections, connection_id, securities = held_fixture(
        settings, with_bitcoin=False
    )
    service = SchwabPriceService(database, connections)
    midday = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    # 14:00Z expressed as 09:00-05:00: later than the live reading, but earlier as raw text.
    manual_time = (midday + timedelta(hours=2)).astimezone(timezone(timedelta(hours=-5)))
    assert manual_time.isoformat() < midday.isoformat()

    asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=loader(
                quote("VTI", "191.25", observed=midday),
                quote("VTSAX", observed=midday),
            ),
        )
    )
    asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=failing_loader(SchwabOAuthError("provider_unavailable")),
            manual_observations=(
                ManualPriceObservation(
                    symbol="VTI",
                    price=Decimal("188.40"),
                    observed_at_utc=manual_time,
                    source="manual_statement",
                ),
            ),
        )
    )

    with closing(database.connect()) as conn:
        stored = [
            str(row["observed_at_utc"])
            for row in conn.execute(
                "SELECT observed_at_utc FROM provider_price_observations"
            ).fetchall()
        ]
    assert all(value.endswith("+00:00") for value in stored), stored

    publication = asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=failing_loader(SchwabOAuthError("provider_unavailable")),
        )
    )

    carried = {item.security_id: item for item in publication.observations}[securities["VTI"]]
    # The newest observation by real time is carried forward, not the one that sorts last as text.
    assert carried.price_status == "carried_forward"
    assert carried.source == "manual_statement"
    assert carried.price == Decimal("188.40000000")
    assert carried.observed_at_utc == manual_time
    assert carried.observed_at_utc.utcoffset() == timedelta(0)


def test_the_bitcoin_source_is_skipped_when_no_bitcoin_is_held(settings: Settings) -> None:
    database, connections, connection_id, _ = held_fixture(settings, with_bitcoin=False)
    service = SchwabPriceService(database, connections)

    async def unreachable() -> BitcoinSpotObservation:
        raise AssertionError("the spot source must not be contacted")

    publication = asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=loader(quote("VTI"), quote("VTSAX")),
            load_bitcoin_spot=unreachable,
        )
    )

    assert publication.bitcoin_source_status == "not_required"
    assert publication.completeness == "complete"


@pytest.mark.parametrize(
    ("returned", "expected"),
    [
        ((), "price_symbol_coverage_conflict"),
        (("VTI",), "price_symbol_coverage_conflict"),
        (("VTI", "VTSAX", "MSFT"), "price_symbol_coverage_conflict"),
    ],
)
def test_a_quote_set_that_does_not_match_the_request_publishes_nothing(
    settings: Settings, returned: tuple[str, ...], expected: str
) -> None:
    database, connections, connection_id, _ = held_fixture(settings, with_bitcoin=False)
    service = SchwabPriceService(database, connections)

    async def load(symbols: tuple[str, ...]) -> tuple[SchwabQuote, ...]:
        del symbols
        return tuple(quote(symbol) for symbol in returned)

    with pytest.raises(PriceRefreshError, match=expected):
        asyncio.run(
            service.refresh(connection_id=connection_id, actor=ACTOR, load_quotes=load)
        )

    assert service.current(connection_id) is None


def test_a_non_quote_response_is_rejected_before_publication(settings: Settings) -> None:
    database, connections, connection_id, _ = held_fixture(settings, with_bitcoin=False)
    service = SchwabPriceService(database, connections)

    async def load(symbols: tuple[str, ...]) -> tuple[SchwabQuote, ...]:
        del symbols
        return ({"symbol": "VTI", "price": "191.25"},)  # type: ignore[return-value]

    with pytest.raises(PriceRefreshError, match="price_quote_response_invalid"):
        asyncio.run(
            service.refresh(connection_id=connection_id, actor=ACTOR, load_quotes=load)
        )

    assert service.current(connection_id) is None


def test_a_refresh_without_held_securities_publishes_nothing(settings: Settings) -> None:
    database, portfolio, connections, connection_id = connected_repository(settings)
    account = portfolio.create_account(
        name="Synthetic empty account", account_type="roth_ira", tax_treatment="tax_free"
    )
    connections.map_account(connection_id, account.id, external_account_hash="synthetic-hash")
    service = SchwabPriceService(database, connections)

    with pytest.raises(PriceRefreshError, match="price_no_held_securities"):
        asyncio.run(
            service.refresh(
                connection_id=connection_id, actor=ACTOR, load_quotes=loader()
            )
        )

    assert service.current(connection_id) is None


def test_a_disconnected_connection_preserves_the_prior_checkpoint(settings: Settings) -> None:
    database, connections, connection_id, _ = held_fixture(settings, with_bitcoin=False)
    service = SchwabPriceService(database, connections)
    asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=loader(quote("VTI"), quote("VTSAX")),
        )
    )
    first = service.current(connection_id)
    connections.disconnect(connection_id)

    with pytest.raises(PriceRefreshError, match="price_connection_changed"):
        asyncio.run(
            service.refresh(
                connection_id=connection_id,
                actor=ACTOR,
                load_quotes=loader(quote("VTI", "1.00"), quote("VTSAX", "1.00")),
            )
        )

    assert first is not None
    current = service.current(connection_id)
    assert current is not None and current.id == first.id


def test_a_concurrent_refresh_is_rejected_for_the_same_connection(settings: Settings) -> None:
    database, connections, connection_id, _ = held_fixture(settings, with_bitcoin=False)
    service = SchwabPriceService(database, connections)
    release = asyncio.Event()

    async def exercise() -> None:
        async def slow(symbols: tuple[str, ...]) -> tuple[SchwabQuote, ...]:
            await release.wait()
            return tuple(quote(symbol) for symbol in symbols)

        first = asyncio.create_task(
            service.refresh(connection_id=connection_id, actor=ACTOR, load_quotes=slow)
        )
        await asyncio.sleep(0)
        with pytest.raises(PriceRefreshError, match="price_refresh_in_progress"):
            await service.refresh(
                connection_id=connection_id, actor=ACTOR, load_quotes=loader(quote("VTI"))
            )
        release.set()
        await first

    asyncio.run(exercise())
    publication = service.current(connection_id)
    assert publication is not None and publication.completeness == "complete"


def test_cancellation_publishes_nothing_and_preserves_prior_state(settings: Settings) -> None:
    database, connections, connection_id, _ = held_fixture(settings, with_bitcoin=False)
    service = SchwabPriceService(database, connections)

    async def exercise() -> None:
        async def hang(symbols: tuple[str, ...]) -> tuple[SchwabQuote, ...]:
            del symbols
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        task = asyncio.create_task(
            service.refresh(connection_id=connection_id, actor=ACTOR, load_quotes=hang)
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert service.current(connection_id) is None
    with closing(database.connect()) as conn:
        run = conn.execute(
            "SELECT status, error_code FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert run["status"] == "failed"
    assert run["error_code"] == "price_refresh_cancelled"


def test_a_failed_refresh_records_a_privacy_safe_sync_run(settings: Settings) -> None:
    database, connections, connection_id, _ = held_fixture(settings, with_bitcoin=False)
    service = SchwabPriceService(database, connections)

    with pytest.raises(PriceRefreshError):
        asyncio.run(
            service.refresh(
                connection_id=connection_id,
                actor=ACTOR,
                load_quotes=loader(quote("VTI")),
            )
        )

    with closing(database.connect()) as conn:
        run = conn.execute(
            "SELECT status, error_code, records_written FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        observations = conn.execute(
            "SELECT COUNT(*) AS total FROM provider_price_observations"
        ).fetchone()
    assert run["status"] == "failed"
    assert run["error_code"] == "price_symbol_coverage_conflict"
    assert run["records_written"] == 0
    assert observations["total"] == 0


def test_only_a_complete_checkpoint_advances_the_last_successful_sync(
    settings: Settings,
) -> None:
    database, connections, connection_id, _ = held_fixture(settings, with_bitcoin=False)
    service = SchwabPriceService(database, connections)

    asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=failing_loader(SchwabOAuthError("provider_unavailable")),
        )
    )
    assert connections.get(connection_id).last_successful_sync_at is None

    asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=loader(quote("VTI"), quote("VTSAX")),
        )
    )
    assert connections.get(connection_id).last_successful_sync_at is not None


def test_the_schema_makes_a_valueless_observation_with_a_price_unrepresentable(
    settings: Settings,
) -> None:
    database, connections, connection_id, securities = held_fixture(
        settings, with_bitcoin=False
    )
    service = SchwabPriceService(database, connections)
    asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=loader(quote("VTI"), quote("VTSAX")),
        )
    )
    publication = service.current(connection_id)
    assert publication is not None

    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction() as conn:
            conn.execute(
                """INSERT INTO provider_price_observations(
                       checkpoint_id, security_id, source, price_status, price,
                       previous_close, observed_at_utc, market_session, quote_context, freshness
                   ) VALUES (?, ?, 'manual_backfill', 'unknown', '0', NULL, ?, NULL, NULL,
                             'fresh')""",
                (
                    publication.id,
                    securities["VTI"],
                    datetime.now(UTC).isoformat(),
                ),
            )


def test_price_checkpoints_survive_an_encrypted_backup_and_isolated_restore(
    settings: Settings, tmp_path
) -> None:
    database, connections, connection_id, securities = held_fixture(
        settings, with_bitcoin=False
    )
    service = SchwabPriceService(database, connections)
    observed = datetime.now(UTC) - timedelta(minutes=15)
    published = asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=loader(
                quote("VTI", "191.25", observed=observed),
                quote("VTSAX", "121.44", observed=observed),
            ),
        )
    )
    backups = BackupService(database, settings.backup_dir, settings.backup_vault())
    archive = backups.create(prune=False)
    restored_path = tmp_path / "restored-prices" / "northstar.db"
    backups.restore_to(archive, restored_path)

    restored_database = database.at_path(restored_path)
    restored_service = SchwabPriceService(
        restored_database, ProviderConnectionRepository(restored_database)
    )
    recovered = restored_service.current(connection_id)

    assert recovered is not None
    assert recovered.id == published.id
    assert recovered.completeness == "complete"
    by_security = {item.security_id: item for item in recovered.observations}
    assert by_security[securities["VTI"]].price == Decimal("191.25000000")
    assert by_security[securities["VTI"]].observed_at_utc == observed


def test_the_price_schema_migration_is_current_and_audit_metadata_is_privacy_safe(
    settings: Settings,
) -> None:
    database, connections, connection_id, _ = held_fixture(settings, with_bitcoin=False)
    service = SchwabPriceService(database, connections)
    asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor=ACTOR,
            load_quotes=loader(quote("VTI", "191.25"), quote("VTSAX", "121.44")),
        )
    )

    version = database.schema_version()
    with closing(database.connect()) as conn:
        checkpoint = conn.execute(
            "SELECT audit_metadata_json FROM provider_price_checkpoints"
        ).fetchone()
        event = conn.execute(
            """SELECT metadata_json FROM audit_events
               WHERE event_type='schwab_prices_published'"""
        ).fetchone()
    assert version == LATEST_SCHEMA_VERSION
    for payload in (checkpoint["audit_metadata_json"], event["metadata_json"]):
        assert all(
            private not in payload for private in ("191.25", "121.44", "VTI", "VTSAX")
        )
    assert '"zero_price_substituted":false' in checkpoint["audit_metadata_json"]
    assert '"completeness":"complete"' in event["metadata_json"]


def test_the_price_service_has_no_route_scheduler_or_provider_client(settings: Settings) -> None:
    database, connections, _, _ = held_fixture(settings, with_bitcoin=False)
    service = SchwabPriceService(database, connections)

    # Every provider read is injected. The service cannot originate a request on its own.
    assert not hasattr(service, "read_client")
    assert not hasattr(service, "oauth_client")
    forbidden = ("order", "trade", "buy", "sell", "transfer", "withdraw", "wallet", "schedule")
    surface = {name for name in dir(service) if not name.startswith("_")}
    assert not any(fragment in name for fragment in forbidden for name in surface)
