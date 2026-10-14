"""One explicitly documented, read-only Bitcoin spot-price source.

This module is deliberately separate from the Schwab integration. It sends no credential,
cookie, identifier, or account detail to the price source, requests exactly one documented
public endpoint, and has no wallet, custody, transfer, or purchase capability.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, NoReturn, Protocol

import httpx

from northstar.domain import DecimalKind, canonical_decimal

# Coinbase Exchange documents this unauthenticated market-data endpoint. It is the only
# Bitcoin price source Northstar is permitted to contact.
BITCOIN_SPOT_SOURCE = "coinbase_exchange_btc_usd_ticker_v1"
BITCOIN_SPOT_URL = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
MAX_SPOT_RESPONSE_BYTES = 64 * 1024
SPOT_READ_TIMEOUT_SECONDS = 10
MAX_SPOT_PRICE = Decimal("100000000")
MAX_SPOT_FUTURE_SKEW = timedelta(minutes=5)
MIN_SPOT_OBSERVATION_TIME = datetime(2009, 1, 3, tzinfo=UTC)
SPOT_PRICE_ERROR_CODES = frozenset(
    {
        "spot_rate_limited",
        "spot_response_invalid",
        "spot_response_too_large",
        "spot_timeout",
        "spot_unavailable",
        "spot_validation_observation_time",
        "spot_validation_price_malformed",
        "spot_validation_price_missing",
        "spot_validation_response_envelope",
    }
)


class SpotPriceValidationCategory(StrEnum):
    RESPONSE_ENVELOPE = "response_envelope"
    PRICE_MISSING = "price_missing"
    PRICE_MALFORMED = "price_malformed"
    OBSERVATION_TIME = "observation_time"


class SpotPriceError(RuntimeError):
    """An allowlisted, privacy-safe spot-price failure."""

    def __init__(self, code: str) -> None:
        if code not in SPOT_PRICE_ERROR_CODES:
            raise ValueError("Spot price error code is not allowlisted")
        super().__init__(code)
        self.code = code


class SpotPriceValidationError(SpotPriceError):
    """A bounded validation failure that never carries a provider value."""

    def __init__(self, category: SpotPriceValidationCategory) -> None:
        super().__init__(f"spot_validation_{category.value}")
        self.category = category


@dataclass(frozen=True)
class BitcoinSpotObservation:
    """One complete spot observation. A missing or unusable price is never represented here."""

    source: str
    price: Decimal = field(repr=False)
    observed_at_utc: datetime = field(repr=False)

    def __post_init__(self) -> None:
        if self.source != BITCOIN_SPOT_SOURCE:
            raise ValueError("Bitcoin spot source is not allowlisted")
        if self.observed_at_utc.tzinfo is None:
            raise ValueError("Bitcoin spot observation time is malformed")
        if self.price <= 0 or self.price > MAX_SPOT_PRICE:
            raise ValueError("Bitcoin spot price is malformed")
        canonical_decimal(self.price, DecimalKind.PRICE, allow_negative=False)


class BitcoinSpotClient(Protocol):
    """Read-only spot surface. Purchases, transfers, and custody are unrepresentable."""

    async def spot_price(self) -> BitcoinSpotObservation: ...


class HttpBitcoinSpotClient:
    """A single-endpoint client with no generic request method and no outbound credential."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            timeout=SPOT_READ_TIMEOUT_SECONDS,
            follow_redirects=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def spot_price(self) -> BitcoinSpotObservation:
        """Read one bounded spot observation, retaining no raw response."""
        try:
            async with self._http.stream(
                "GET",
                BITCOIN_SPOT_URL,
                # No Authorization, cookie, identifier, or account detail is ever sent.
                headers={"Accept": "application/json"},
            ) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_SPOT_RESPONSE_BYTES:
                        raise SpotPriceError("spot_response_too_large")
                    content.extend(chunk)
        except httpx.TimeoutException:
            raise SpotPriceError("spot_timeout") from None
        except httpx.RequestError:
            raise SpotPriceError("spot_unavailable") from None
        if response.status_code != 200:
            raise SpotPriceError(_spot_status_code(response.status_code))
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise SpotPriceValidationError(SpotPriceValidationCategory.RESPONSE_ENVELOPE)
        try:
            payload = json.loads(content, parse_float=Decimal, parse_int=Decimal)
            return _spot_observation(payload, imported_at=datetime.now(UTC))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SpotPriceValidationError(
                SpotPriceValidationCategory.RESPONSE_ENVELOPE
            ) from None
        finally:
            content.clear()


def _spot_observation(payload: Any, *, imported_at: datetime) -> BitcoinSpotObservation:
    if not isinstance(payload, dict):
        _spot_validation(SpotPriceValidationCategory.RESPONSE_ENVELOPE)
    if "price" not in payload:
        _spot_validation(SpotPriceValidationCategory.PRICE_MISSING)
    if "time" not in payload:
        _spot_validation(SpotPriceValidationCategory.OBSERVATION_TIME)
    price = _spot_price(payload["price"])
    observed = _spot_observed_at(payload["time"], imported_at=imported_at)
    try:
        return BitcoinSpotObservation(
            source=BITCOIN_SPOT_SOURCE, price=price, observed_at_utc=observed
        )
    except (TypeError, ValueError):
        _spot_validation(SpotPriceValidationCategory.PRICE_MALFORMED)


def _spot_price(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        _spot_validation(SpotPriceValidationCategory.PRICE_MALFORMED)
    try:
        price = Decimal(canonical_decimal(value, DecimalKind.PRICE, allow_negative=False))
    except (TypeError, ValueError):
        _spot_validation(SpotPriceValidationCategory.PRICE_MALFORMED)
    if price <= 0 or price > MAX_SPOT_PRICE:
        # An absent, zero, or implausible spot price must never become a valuation input.
        _spot_validation(SpotPriceValidationCategory.PRICE_MALFORMED)
    return price


def _spot_observed_at(value: Any, *, imported_at: datetime) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        _spot_validation(SpotPriceValidationCategory.OBSERVATION_TIME)
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _spot_validation(SpotPriceValidationCategory.OBSERVATION_TIME)
    if observed.tzinfo is None:
        _spot_validation(SpotPriceValidationCategory.OBSERVATION_TIME)
    observed = observed.astimezone(UTC)
    if (
        observed < MIN_SPOT_OBSERVATION_TIME
        or observed > imported_at + MAX_SPOT_FUTURE_SKEW
    ):
        _spot_validation(SpotPriceValidationCategory.OBSERVATION_TIME)
    return observed


def _spot_status_code(status_code: int) -> str:
    if status_code == 429:
        return "spot_rate_limited"
    if status_code >= 500:
        return "spot_unavailable"
    return "spot_response_invalid"


def _spot_validation(category: SpotPriceValidationCategory) -> NoReturn:
    raise SpotPriceValidationError(category)
