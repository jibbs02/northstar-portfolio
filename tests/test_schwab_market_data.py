from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from northstar.integrations import (
    BITCOIN_SPOT_SOURCE,
    BitcoinSpotClient,
    BitcoinSpotObservation,
    HttpBitcoinSpotClient,
    HttpSchwabReadClient,
    SchwabOAuthError,
    SchwabQuote,
    SchwabReadClient,
    SpotPriceError,
    SpotPriceValidationError,
)
from northstar.integrations.bitcoin import (
    BITCOIN_SPOT_URL,
    MAX_SPOT_PRICE,
    MAX_SPOT_RESPONSE_BYTES,
    SPOT_PRICE_ERROR_CODES,
)
from northstar.integrations.schwab import (
    _QUOTE_VARIANTS,
    MAX_QUOTE_RESPONSE_BYTES,
    MAX_QUOTE_SYMBOLS,
    SCHWAB_MARKET_DATA_BASE_URL,
    SCHWAB_OAUTH_ERROR_CODES,
    SCHWAB_QUOTE_ASSET_MAIN_TYPES,
)

# Held in a plain local so the credential linter does not treat the synthetic placeholder
# as a hardcoded secret. No real credential exists anywhere in this repository.
ACCESS_TOKEN = "synthetic-placeholder-access-credential"  # noqa: S105 - synthetic fixture
# Fixed synthetic observation times, deliberately in the past so no fixture depends on
# the wall clock being inside the documented future-skew allowance.
OBSERVED_AT = datetime(2026, 8, 14, 19, 55, tzinfo=UTC)
OBSERVED_TEXT = "2026-08-14T19:55:00.123456Z"
PRIVATE_VALUES = ("191.25", "190.1", "121.44", "64125.37", "AAPL", "VTSAX")


def epoch_millis(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def equity_quote(
    symbol: str = "AAPL",
    *,
    observed: datetime | None = None,
    **overrides: object,
) -> dict[str, object]:
    quote: dict[str, object] = {
        "lastPrice": 191.25,
        "closePrice": 190.10,
        "quoteTime": epoch_millis(observed or datetime.now(UTC)),
        "securityStatus": "Normal",
        # Undocumented additive members must be ignored, never treated as an alternate price.
        "privateProviderField": "synthetic-private-quote-detail",
        "lastSize": 100,
    }
    quote.update(overrides)
    return {
        "symbol": symbol,
        "assetMainType": "EQUITY",
        "assetSubType": "COE",
        "quoteType": "NBBO",
        "realtime": True,
        "quote": quote,
    }


def fund_quote(
    symbol: str = "VTSAX",
    *,
    observed: datetime | None = None,
    **overrides: object,
) -> dict[str, object]:
    quote: dict[str, object] = {
        "nAV": 121.44,
        "closePrice": 121.44,
        "tradeTime": epoch_millis(observed or datetime.now(UTC)),
        "securityStatus": "Closed",
    }
    quote.update(overrides)
    return {
        "symbol": symbol,
        "assetMainType": "MUTUAL_FUND",
        "quoteType": "NFL",
        "quote": quote,
    }


def quote_client(handler) -> HttpSchwabReadClient:
    return HttpSchwabReadClient(transport=httpx.MockTransport(handler))


def json_response(payload: object) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "application/json; charset=utf-8"}, json=payload
    )


def read_quotes(payload: object, symbols: tuple[str, ...]) -> tuple[SchwabQuote, ...]:
    async def exercise() -> tuple[SchwabQuote, ...]:
        client = quote_client(lambda _: json_response(payload))
        try:
            return await client.quotes(access_token=ACCESS_TOKEN, symbols=symbols)
        finally:
            await client.close()

    return asyncio.run(exercise())


def expect_quote_failure(
    payload: object,
    symbols: tuple[str, ...],
    expected: str,
) -> None:
    async def exercise() -> None:
        client = quote_client(lambda _: json_response(payload))
        try:
            with pytest.raises(SchwabOAuthError, match=expected) as failure:
                await client.quotes(access_token=ACCESS_TOKEN, symbols=symbols)
        finally:
            await client.close()
        assert failure.value.code in SCHWAB_OAUTH_ERROR_CODES
        rendered = str(failure.value)
        assert all(private not in rendered for private in PRIVATE_VALUES)

    asyncio.run(exercise())


def test_quote_transport_uses_only_the_documented_bounded_market_data_endpoint() -> None:
    requests: list[httpx.Request] = []
    observed = OBSERVED_AT

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response(
            {
                "AAPL": equity_quote(observed=observed),
                "VTSAX": fund_quote(observed=observed),
            }
        )

    async def exercise() -> tuple[SchwabQuote, ...]:
        client = quote_client(handler)
        try:
            return await client.quotes(
                access_token=ACCESS_TOKEN, symbols=("AAPL", "VTSAX")
            )
        finally:
            await client.close()

    quotes = asyncio.run(exercise())
    request = requests[0]
    assert len(requests) == 1
    assert request.method == "GET"
    assert str(request.url).startswith(f"{SCHWAB_MARKET_DATA_BASE_URL}/quotes")
    assert request.url.path == "/marketdata/v1/quotes"
    assert request.url.params["symbols"] == "AAPL,VTSAX"
    assert request.url.params["fields"] == "quote"
    assert request.url.params["indicative"] == "false"
    # A quote request carries no account, mapping, or ordering parameter of any kind.
    assert set(request.url.params) == {"symbols", "fields", "indicative"}

    assert tuple(quote.symbol for quote in quotes) == ("AAPL", "VTSAX")
    equity, fund = quotes
    assert equity.asset_main_type == "EQUITY"
    assert equity.quote_context == "NBBO"
    assert equity.market_session == "normal"
    assert equity.price == Decimal("191.25000000")
    assert equity.previous_close == Decimal("190.10000000")
    assert equity.observed_at_utc == observed
    assert fund.asset_main_type == "MUTUAL_FUND"
    assert fund.quote_context == "NFL"
    assert fund.market_session == "closed"
    assert fund.price == Decimal("121.44000000")
    assert fund.observed_at_utc == observed

    rendered = repr(quotes)
    assert all(private not in rendered for private in PRIVATE_VALUES)
    assert "synthetic-private-quote-detail" not in rendered
    assert "2026-08-14" not in rendered


def test_quote_transport_preserves_a_stale_observation_time_without_substituting_now() -> None:
    stale = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)

    quotes = read_quotes({"AAPL": equity_quote(observed=stale)}, ("AAPL",))

    # Staleness is the caller's decision; the transport must never relabel an old observation.
    assert quotes[0].observed_at_utc == stale
    assert datetime.now(UTC) - quotes[0].observed_at_utc > timedelta(days=1)


@pytest.mark.parametrize(
    "symbols",
    [
        (),
        ("AAPL", "AAPL"),
        ("aapl",),
        ("AAPL ",),
        ("AA PL",),
        ("A" * 33,),
        ("AAPL;DROP",),
        ("",),
        (b"AAPL",),
        ("AAPL", None),
        tuple(f"SYM{index}" for index in range(MAX_QUOTE_SYMBOLS + 1)),
    ],
)
def test_quote_transport_rejects_invalid_symbol_batches_before_network(symbols: tuple) -> None:
    async def exercise() -> None:
        client = quote_client(lambda _: (_ for _ in ()).throw(AssertionError()))
        try:
            with pytest.raises(ValueError, match="symbol batch"):
                await client.quotes(access_token=ACCESS_TOKEN, symbols=symbols)
        finally:
            await client.close()

    asyncio.run(exercise())


def test_quote_transport_rejects_a_non_tuple_symbol_collection_before_network() -> None:
    async def exercise() -> None:
        client = quote_client(lambda _: (_ for _ in ()).throw(AssertionError()))
        try:
            with pytest.raises(ValueError, match="symbol batch"):
                await client.quotes(access_token=ACCESS_TOKEN, symbols=["AAPL"])
        finally:
            await client.close()

    asyncio.run(exercise())


def test_quote_transport_accepts_the_documented_maximum_batch() -> None:
    symbols = tuple(f"SYM{index}" for index in range(MAX_QUOTE_SYMBOLS))
    payload = {symbol: equity_quote(symbol) for symbol in symbols}

    quotes = read_quotes(payload, symbols)

    assert tuple(quote.symbol for quote in quotes) == symbols


@pytest.mark.parametrize(
    ("payload", "symbols", "expected"),
    [
        # Partial coverage can never be mistaken for a zero-priced holding.
        ({"AAPL": equity_quote()}, ("AAPL", "VTSAX"), "quote_symbol_coverage"),
        (
            {"AAPL": equity_quote(), "MSFT": equity_quote("MSFT")},
            ("AAPL",),
            "quote_symbol_coverage",
        ),
        ({}, ("AAPL",), "quote_symbol_coverage"),
        # The keyed symbol and the inner symbol member must agree.
        ({"AAPL": equity_quote("MSFT")}, ("AAPL",), "quote_symbol_coverage"),
        ({"AAPL": {"assetMainType": "EQUITY"}}, ("AAPL",), "quote_symbol_coverage"),
        ({"AAPL": {"symbol": "AAPL", "assetMainType": "EQUITY"}}, ("AAPL",), "quote_structure"),
        ({"AAPL": "not-an-object"}, ("AAPL",), "quote_structure"),
        (
            {"AAPL": {"symbol": "AAPL", "assetMainType": "EQUITY", "quote": []}},
            ("AAPL",),
            "quote_structure",
        ),
        ([{"symbol": "AAPL"}], ("AAPL",), "response_envelope"),
    ],
)
def test_quote_transport_requires_exact_symbol_coverage_and_documented_structure(
    payload: object, symbols: tuple[str, ...], expected: str
) -> None:
    expect_quote_failure(payload, symbols, expected)


@pytest.mark.parametrize(
    "errors",
    [
        {"invalidSymbols": ["AAPL"]},
        {"invalidCusips": ["037833100"]},
        [{"message": "synthetic"}],
    ],
)
def test_quote_transport_fails_closed_when_the_provider_reports_symbol_errors(
    errors: object,
) -> None:
    payload = {"VTSAX": fund_quote(), "errors": errors}

    expect_quote_failure(payload, ("VTSAX",), "provider_quote_symbol_unavailable")


def test_quote_transport_ignores_an_empty_errors_member() -> None:
    quotes = read_quotes({"VTSAX": fund_quote(), "errors": {}}, ("VTSAX",))

    assert quotes[0].symbol == "VTSAX"


@pytest.mark.parametrize(
    ("quote_payload", "expected"),
    [
        # No documented price member at all is missing, not malformed.
        ({"symbol": "AAPL", "assetMainType": "EQUITY", "quoteType": "NBBO", "quote": {}},
         "quote_variant_missing"),
        # A partial documented representation is malformed rather than silently halved.
        (
            {
                "symbol": "AAPL",
                "assetMainType": "EQUITY",
                "quoteType": "NBBO",
                "quote": {"lastPrice": 191.25, "quoteTime": 0, "securityStatus": "Normal"},
            },
            "quote_variant_malformed",
        ),
        (
            {
                "symbol": "VTSAX",
                "assetMainType": "MUTUAL_FUND",
                "quoteType": "NFL",
                "quote": {"closePrice": 121.44, "tradeTime": 0, "securityStatus": "Closed"},
            },
            "quote_variant_malformed",
        ),
    ],
)
def test_quote_transport_separates_missing_from_partial_price_variants(
    quote_payload: dict[str, object], expected: str
) -> None:
    expect_quote_failure({quote_payload["symbol"]: quote_payload},
                         (str(quote_payload["symbol"]),), expected)


@pytest.mark.parametrize(
    "overrides",
    [
        {"lastPrice": 0},
        {"lastPrice": "0.00"},
        {"lastPrice": -191.25},
        {"closePrice": 0},
        {"lastPrice": "not-a-number"},
        {"lastPrice": True},
        {"lastPrice": None},
        {"lastPrice": {"amount": 191.25}},
        {"lastPrice": "NaN"},
        {"lastPrice": "Infinity"},
    ],
)
def test_quote_transport_never_accepts_a_zero_or_unusable_price(
    overrides: dict[str, object],
) -> None:
    expect_quote_failure(
        {"AAPL": equity_quote(**overrides)}, ("AAPL",), "quote_variant_malformed"
    )


@pytest.mark.parametrize(
    ("asset_main_type", "expected"),
    [
        (None, "quote_asset_type_malformed"),
        ("", "quote_asset_type_malformed"),
        (" EQUITY", "quote_asset_type_malformed"),
        ("equity", "quote_asset_type_malformed"),
        (17, "quote_asset_type_malformed"),
        ("OPTION", "quote_asset_type_unsupported"),
        ("FUTURE", "quote_asset_type_unsupported"),
        ("FOREX", "quote_asset_type_unsupported"),
        ("INDEX", "quote_asset_type_unsupported"),
        ("BOND", "quote_asset_type_unsupported"),
    ],
)
def test_quote_transport_separates_malformed_from_unsupported_asset_types(
    asset_main_type: object, expected: str
) -> None:
    payload = equity_quote()
    if asset_main_type is None:
        del payload["assetMainType"]
    else:
        payload["assetMainType"] = asset_main_type

    expect_quote_failure({"AAPL": payload}, ("AAPL",), expected)


@pytest.mark.parametrize("quote_type", [None, "", "REALTIME", "nbbo", 4, "NBBO "])
def test_quote_transport_requires_a_documented_quote_context(quote_type: object) -> None:
    payload = equity_quote()
    if quote_type is None:
        del payload["quoteType"]
    else:
        payload["quoteType"] = quote_type

    expect_quote_failure({"AAPL": payload}, ("AAPL",), "quote_context")


@pytest.mark.parametrize(
    ("security_status", "expected"),
    [
        ("__missing__", "quote_variant_missing"),
        ("", "quote_session_malformed"),
        (12, "quote_session_malformed"),
        (" Normal", "quote_session_malformed"),
        ("Auction", "quote_session_unsupported"),
        ("Unknown", "quote_session_unsupported"),
    ],
)
def test_quote_transport_requires_documented_market_session_context(
    security_status: object, expected: str
) -> None:
    payload = equity_quote()
    if security_status == "__missing__":
        del payload["quote"]["securityStatus"]  # type: ignore[index]
    else:
        payload["quote"]["securityStatus"] = security_status  # type: ignore[index]

    expect_quote_failure({"AAPL": payload}, ("AAPL",), expected)


@pytest.mark.parametrize("status", ["Normal", "NORMAL", "normal", "Halted", "Closed"])
def test_quote_transport_normalizes_documented_session_names(status: str) -> None:
    quotes = read_quotes({"AAPL": equity_quote(securityStatus=status)}, ("AAPL",))

    assert quotes[0].market_session == status.lower()


@pytest.mark.parametrize(
    "quote_time",
    [
        "__missing__",
        "1755431100000",
        191.25,
        None,
        True,
        0,
        -1,
        946684799000,
        epoch_millis(datetime.now(UTC) + timedelta(hours=1)),
        10**18,
    ],
)
def test_quote_transport_rejects_missing_or_implausible_observation_times(
    quote_time: object,
) -> None:
    payload = equity_quote()
    if quote_time == "__missing__":
        del payload["quote"]["quoteTime"]  # type: ignore[index]
        expected = "quote_variant_missing"
    else:
        payload["quote"]["quoteTime"] = quote_time  # type: ignore[index]
        expected = "quote_observation_time"

    expect_quote_failure({"AAPL": payload}, ("AAPL",), expected)


def test_quote_transport_accepts_an_observation_inside_the_documented_future_skew() -> None:
    skewed = datetime.now(UTC) + timedelta(minutes=2)

    quotes = read_quotes({"AAPL": equity_quote(observed=skewed)}, ("AAPL",))

    assert quotes[0].observed_at_utc <= skewed


def test_quote_transport_records_conflicting_observations_for_each_symbol_separately() -> None:
    older = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)
    newer = OBSERVED_AT
    payload = {
        "AAPL": equity_quote(observed=newer),
        "VTSAX": fund_quote(observed=older, securityStatus="Halted"),
    }

    quotes = read_quotes(payload, ("AAPL", "VTSAX"))

    # Two sources of the same batch may disagree on time and session; neither is normalized away.
    assert quotes[0].observed_at_utc == newer
    assert quotes[0].market_session == "normal"
    assert quotes[1].observed_at_utc == older
    assert quotes[1].market_session == "halted"


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, "provider_authorization_rejected"),
        (403, "provider_authorization_rejected"),
        (429, "provider_rate_limited"),
        (500, "provider_unavailable"),
        (503, "provider_unavailable"),
        (400, "provider_authorization_rejected"),
        (404, "provider_validation_response_envelope"),
    ],
)
def test_quote_transport_translates_only_privacy_safe_provider_failures(
    status_code: int, expected: str
) -> None:
    async def exercise() -> None:
        client = quote_client(
            lambda _: httpx.Response(
                status_code,
                headers={"content-type": "application/json"},
                json={"message": "synthetic provider detail 191.25"},
            )
        )
        try:
            with pytest.raises(SchwabOAuthError, match=expected) as failure:
                await client.quotes(access_token=ACCESS_TOKEN, symbols=("AAPL",))
        finally:
            await client.close()
        assert failure.value.code in SCHWAB_OAUTH_ERROR_CODES
        assert "synthetic provider detail" not in str(failure.value)

    asyncio.run(exercise())


def test_quote_transport_rejects_wrong_content_type_oversize_timeout_outage_and_cancellation() -> None:
    class OversizedQuoteStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * (MAX_QUOTE_RESPONSE_BYTES + 1)

        async def aclose(self) -> None:
            return None

    async def exercise() -> None:
        clients = (
            (
                quote_client(lambda _: httpx.Response(200, text="{}")),
                "provider_validation_response_envelope",
            ),
            (
                quote_client(
                    lambda _: httpx.Response(
                        200,
                        headers={"content-type": "application/json"},
                        content=b"{not json",
                    )
                ),
                "provider_validation_response_envelope",
            ),
            (
                quote_client(
                    lambda _: httpx.Response(
                        200,
                        headers={"content-type": "application/json"},
                        stream=OversizedQuoteStream(),
                    )
                ),
                "provider_response_too_large",
            ),
            (
                quote_client(lambda _: (_ for _ in ()).throw(httpx.ReadTimeout("synthetic"))),
                "provider_timeout",
            ),
            (
                quote_client(lambda _: (_ for _ in ()).throw(httpx.ConnectError("synthetic"))),
                "provider_unavailable",
            ),
        )
        try:
            for client, expected in clients:
                with pytest.raises(SchwabOAuthError, match=expected):
                    await client.quotes(access_token=ACCESS_TOKEN, symbols=("AAPL",))
        finally:
            for client, _ in clients:
                await client.close()

        cancelled = quote_client(
            lambda _: (_ for _ in ()).throw(asyncio.CancelledError())
        )
        try:
            with pytest.raises(asyncio.CancelledError):
                await cancelled.quotes(access_token=ACCESS_TOKEN, symbols=("AAPL",))
        finally:
            await cancelled.close()

    asyncio.run(exercise())


def test_quote_transport_requires_a_credential_and_sends_no_account_detail() -> None:
    requests: list[httpx.Request] = []

    blank = "  "

    async def exercise() -> None:
        client = quote_client(
            lambda request: (requests.append(request), json_response({"AAPL": equity_quote()}))[1]
        )
        try:
            with pytest.raises(ValueError, match="access token"):
                await client.quotes(access_token=blank, symbols=("AAPL",))
            await client.quotes(access_token=ACCESS_TOKEN, symbols=("AAPL",))
        finally:
            await client.close()

    asyncio.run(exercise())
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert "cookie" not in {name.lower() for name in requests[0].headers}
    assert requests[0].content == b""


def test_quote_model_is_immutable_and_rejects_incomplete_observations() -> None:
    quote = SchwabQuote(
        symbol="AAPL",
        asset_main_type="EQUITY",
        price=Decimal("191.25"),
        previous_close=Decimal("190.10"),
        observed_at_utc=OBSERVED_AT,
        quote_context="NBBO",
        market_session="normal",
    )
    with pytest.raises(FrozenInstanceError):
        quote.price = Decimal("1")  # type: ignore[misc]

    invalid = (
        {"price": Decimal("0")},
        {"price": Decimal("-1")},
        {"previous_close": Decimal("0")},
        {"symbol": "aapl"},
        {"symbol": ""},
        {"asset_main_type": "OPTION"},
        {"quote_context": "REALTIME"},
        {"market_session": "premarket"},
        {"observed_at_utc": OBSERVED_AT.replace(tzinfo=None)},
    )
    for override in invalid:
        fields_ = {
            "symbol": "AAPL",
            "asset_main_type": "EQUITY",
            "price": Decimal("191.25"),
            "previous_close": Decimal("190.10"),
            "observed_at_utc": OBSERVED_AT,
            "quote_context": "NBBO",
            "market_session": "normal",
            **override,
        }
        with pytest.raises(ValueError):
            SchwabQuote(**fields_)  # type: ignore[arg-type]


def test_every_supported_quote_asset_family_declares_its_documented_members() -> None:
    # A supported asset family without a variant entry would raise an unhandled KeyError
    # instead of a privacy-safe category.
    assert set(_QUOTE_VARIANTS) == set(SCHWAB_QUOTE_ASSET_MAIN_TYPES)
    for names, observation_member in _QUOTE_VARIANTS.values():
        assert len(names) == 2
        assert observation_member not in names


def test_market_data_contracts_cannot_represent_orders_or_money_movement() -> None:
    surfaces = {
        name
        for contract in (SchwabReadClient, BitcoinSpotClient)
        for name in contract.__dict__
        if not name.startswith("_")
    }
    forbidden = ("order", "trade", "cancel", "replace", "place", "buy", "sell", "transfer",
                 "withdraw", "deposit", "wallet")

    assert "quotes" in surfaces
    assert "spot_price" in surfaces
    assert not any(fragment in name for fragment in forbidden for name in surfaces)


def spot_client(handler) -> HttpBitcoinSpotClient:
    return HttpBitcoinSpotClient(transport=httpx.MockTransport(handler))


def spot_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "trade_id": 4321,
        "price": "64125.37",
        "size": "0.00412000",
        "time": OBSERVED_TEXT,
        "bid": "64125.00",
        "ask": "64125.50",
        "volume": "1234.50000000",
    }
    payload.update(overrides)
    return payload


def read_spot(payload: object) -> BitcoinSpotObservation:
    async def exercise() -> BitcoinSpotObservation:
        client = spot_client(lambda _: json_response(payload))
        try:
            return await client.spot_price()
        finally:
            await client.close()

    return asyncio.run(exercise())


def expect_spot_failure(payload: object, expected: str) -> None:
    async def exercise() -> None:
        client = spot_client(lambda _: json_response(payload))
        try:
            with pytest.raises(SpotPriceError, match=expected) as failure:
                await client.spot_price()
        finally:
            await client.close()
        assert failure.value.code in SPOT_PRICE_ERROR_CODES
        assert all(private not in str(failure.value) for private in PRIVATE_VALUES)

    asyncio.run(exercise())


def test_bitcoin_spot_transport_uses_one_documented_credential_free_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response(spot_payload())

    async def exercise() -> BitcoinSpotObservation:
        client = spot_client(handler)
        try:
            return await client.spot_price()
        finally:
            await client.close()

    observation = asyncio.run(exercise())
    request = requests[0]
    assert len(requests) == 1
    assert request.method == "GET"
    assert str(request.url) == BITCOIN_SPOT_URL
    assert not request.url.params
    # No credential, cookie, or identifier may reach a third-party price source.
    sent = {name.lower() for name in request.headers}
    assert "authorization" not in sent
    assert "cookie" not in sent
    assert request.content == b""

    assert observation.source == BITCOIN_SPOT_SOURCE
    assert observation.price == Decimal("64125.37000000")
    assert observation.observed_at_utc == datetime(2026, 8, 14, 19, 55, 0, 123456, tzinfo=UTC)
    rendered = repr(observation)
    assert all(private not in rendered for private in PRIVATE_VALUES)
    assert "2026-08-14" not in rendered


def test_bitcoin_spot_transport_preserves_a_stale_observation_time() -> None:
    observation = read_spot(spot_payload(time="2026-07-01T00:00:00Z"))

    assert observation.observed_at_utc == datetime(2026, 7, 1, tzinfo=UTC)
    assert datetime.now(UTC) - observation.observed_at_utc > timedelta(days=1)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"time": OBSERVED_TEXT}, "spot_validation_price_missing"),
        ({"price": "64125.37"}, "spot_validation_observation_time"),
        ({}, "spot_validation_price_missing"),
        ([], "spot_validation_response_envelope"),
        ("64125.37", "spot_validation_response_envelope"),
    ],
)
def test_bitcoin_spot_transport_requires_a_complete_documented_observation(
    payload: object, expected: str
) -> None:
    expect_spot_failure(payload, expected)


@pytest.mark.parametrize(
    "price",
    [
        "0",
        "0.00",
        "-64125.37",
        "not-a-number",
        "",
        None,
        True,
        {"amount": "64125.37"},
        "NaN",
        "Infinity",
        str(MAX_SPOT_PRICE + 1),
    ],
)
def test_bitcoin_spot_transport_never_accepts_a_zero_or_implausible_price(
    price: object,
) -> None:
    expect_spot_failure(spot_payload(price=price), "spot_validation_price_malformed")


@pytest.mark.parametrize(
    "moment",
    [
        "2026-08-17T13:45:00",
        "not-a-timestamp",
        "",
        None,
        1755431100,
        "2008-01-01T00:00:00Z",
        "x" * 65,
    ],
)
def test_bitcoin_spot_transport_rejects_missing_or_implausible_observation_times(
    moment: object,
) -> None:
    expect_spot_failure(spot_payload(time=moment), "spot_validation_observation_time")


def test_bitcoin_spot_transport_rejects_a_future_observation_beyond_the_documented_skew() -> None:
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    expect_spot_failure(spot_payload(time=future), "spot_validation_observation_time")


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (429, "spot_rate_limited"),
        (500, "spot_unavailable"),
        (503, "spot_unavailable"),
        (400, "spot_response_invalid"),
        (404, "spot_response_invalid"),
        (401, "spot_response_invalid"),
    ],
)
def test_bitcoin_spot_transport_translates_only_privacy_safe_failures(
    status_code: int, expected: str
) -> None:
    async def exercise() -> None:
        client = spot_client(
            lambda _: httpx.Response(
                status_code,
                headers={"content-type": "application/json"},
                json={"message": "synthetic outage detail 64125.37"},
            )
        )
        try:
            with pytest.raises(SpotPriceError, match=expected) as failure:
                await client.spot_price()
        finally:
            await client.close()
        assert failure.value.code in SPOT_PRICE_ERROR_CODES
        assert "synthetic outage detail" not in str(failure.value)

    asyncio.run(exercise())


def test_bitcoin_spot_transport_rejects_wrong_content_type_oversize_outage_and_cancellation() -> None:
    class OversizedSpotStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * (MAX_SPOT_RESPONSE_BYTES + 1)

        async def aclose(self) -> None:
            return None

    async def exercise() -> None:
        clients = (
            (
                spot_client(lambda _: httpx.Response(200, text="{}")),
                "spot_validation_response_envelope",
            ),
            (
                spot_client(
                    lambda _: httpx.Response(
                        200,
                        headers={"content-type": "application/json"},
                        content=b"{not json",
                    )
                ),
                "spot_validation_response_envelope",
            ),
            (
                spot_client(
                    lambda _: httpx.Response(
                        200,
                        headers={"content-type": "application/json"},
                        stream=OversizedSpotStream(),
                    )
                ),
                "spot_response_too_large",
            ),
            (
                spot_client(lambda _: (_ for _ in ()).throw(httpx.ReadTimeout("synthetic"))),
                "spot_timeout",
            ),
            (
                spot_client(lambda _: (_ for _ in ()).throw(httpx.ConnectError("synthetic"))),
                "spot_unavailable",
            ),
        )
        try:
            for client, expected in clients:
                with pytest.raises(SpotPriceError, match=expected):
                    await client.spot_price()
        finally:
            for client, _ in clients:
                await client.close()

        cancelled = spot_client(lambda _: (_ for _ in ()).throw(asyncio.CancelledError()))
        try:
            with pytest.raises(asyncio.CancelledError):
                await cancelled.spot_price()
        finally:
            await cancelled.close()

    asyncio.run(exercise())


def test_bitcoin_spot_observation_is_immutable_and_source_pinned() -> None:
    observation = BitcoinSpotObservation(
        source=BITCOIN_SPOT_SOURCE,
        price=Decimal("64125.37"),
        observed_at_utc=OBSERVED_AT,
    )
    with pytest.raises(FrozenInstanceError):
        observation.price = Decimal("1")  # type: ignore[misc]

    invalid = (
        {"source": "some_other_price_feed"},
        {"price": Decimal("0")},
        {"price": Decimal("-1")},
        {"price": MAX_SPOT_PRICE + 1},
        {"observed_at_utc": OBSERVED_AT.replace(tzinfo=None)},
    )
    for override in invalid:
        fields_ = {
            "source": BITCOIN_SPOT_SOURCE,
            "price": Decimal("64125.37"),
            "observed_at_utc": OBSERVED_AT,
            **override,
        }
        with pytest.raises(ValueError):
            BitcoinSpotObservation(**fields_)  # type: ignore[arg-type]


def test_spot_price_errors_are_allowlisted_and_never_carry_provider_values() -> None:
    with pytest.raises(ValueError, match="allowlisted"):
        SpotPriceError("spot_price_is_64125.37")

    for code in SPOT_PRICE_ERROR_CODES:
        assert SpotPriceError(code).code == code
    assert issubclass(SpotPriceValidationError, SpotPriceError)
