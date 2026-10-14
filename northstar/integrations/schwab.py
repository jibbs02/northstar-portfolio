from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, NoReturn, Protocol
from urllib.parse import quote, urlencode

import httpx

from northstar.domain import DecimalKind, canonical_decimal

SCHWAB_AUTHORIZE_URL = "https://api.schwabapi.com/v1/oauth/authorize"
SCHWAB_TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"  # noqa: S105 - endpoint URL
SCHWAB_TRADER_BASE_URL = "https://api.schwabapi.com/trader/v1"
SCHWAB_MARKET_DATA_BASE_URL = "https://api.schwabapi.com/marketdata/v1"
MAX_OAUTH_RESPONSE_BYTES = 64 * 1024
MAX_DISCOVERY_RESPONSE_BYTES = 64 * 1024
MAX_SNAPSHOT_RESPONSE_BYTES = 512 * 1024
MAX_TRANSACTION_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_QUOTE_RESPONSE_BYTES = 512 * 1024
SCHWAB_READ_TIMEOUT_SECONDS = 10
MAX_DISCOVERED_ACCOUNTS = 50
MAX_SNAPSHOT_POSITIONS = 2_000
MAX_TRANSACTIONS = 3_000
MAX_TRANSACTION_ITEMS = 32
MAX_TRANSACTION_RANGE = timedelta(days=365)
MAX_QUOTE_SYMBOLS = 50
MAX_QUOTE_SYMBOL_LENGTH = 32
MAX_OBSERVATION_FUTURE_SKEW = timedelta(minutes=5)
MIN_OBSERVATION_TIME = datetime(2000, 1, 1, tzinfo=UTC)
REFRESH_TOKEN_LIFETIME = timedelta(days=7)
BEARER_TOKEN_TYPE = "Bearer"  # noqa: S105 - OAuth token type, not a credential
SCHWAB_OAUTH_ERROR_CODES = frozenset(
    {
        "provider_authorization_rejected",
        "provider_quote_symbol_unavailable",
        "provider_rate_limited",
        "provider_response_invalid",
        "provider_response_too_large",
        "provider_transaction_history_incomplete",
        "provider_timeout",
        "provider_unavailable",
        "provider_validation_account_identity_mechanics",
        "provider_validation_balance_structure",
        "provider_validation_balance_variant_malformed",
        "provider_validation_balance_variant_missing",
        "provider_validation_instrument_identity",
        "provider_validation_instrument_identity_type",
        "provider_validation_instrument_metadata",
        "provider_validation_instrument_structure",
        "provider_validation_instrument_subtype_malformed",
        "provider_validation_instrument_subtype_unsupported",
        "provider_validation_instrument_type",
        "provider_validation_instrument_type_malformed",
        "provider_validation_instrument_type_unsupported",
        "provider_validation_numeric_value",
        "provider_validation_position_structure",
        "provider_validation_quote_asset_type_malformed",
        "provider_validation_quote_asset_type_unsupported",
        "provider_validation_quote_context",
        "provider_validation_quote_observation_time",
        "provider_validation_quote_session_malformed",
        "provider_validation_quote_session_unsupported",
        "provider_validation_quote_structure",
        "provider_validation_quote_symbol_coverage",
        "provider_validation_quote_variant_malformed",
        "provider_validation_quote_variant_missing",
        "provider_validation_response_envelope",
        "provider_validation_transaction_structure",
        "provider_validation_unsupported_borrowing_short_position",
    }
)
SCHWAB_TRANSACTION_TYPES = (
    "TRADE",
    "RECEIVE_AND_DELIVER",
    "DIVIDEND_OR_INTEREST",
    "ACH_RECEIPT",
    "ACH_DISBURSEMENT",
    "CASH_RECEIPT",
    "CASH_DISBURSEMENT",
    "ELECTRONIC_FUND",
    "WIRE_OUT",
    "WIRE_IN",
    "JOURNAL",
    "MEMORANDUM",
    "MARGIN_CALL",
    "MONEY_MARKET",
    "SMA_ADJUSTMENT",
)
SCHWAB_TRANSACTION_STATUSES = frozenset({"VALID", "INVALID", "PENDING", "UNKNOWN"})
SCHWAB_TRANSACTION_ACTIVITY_TYPES = frozenset(
    {"ACTIVITY_CORRECTION", "EXECUTION", "ORDER_ACTION", "TRANSFER", "UNKNOWN"}
)
SCHWAB_TRANSACTION_FEE_TYPES = frozenset(
    {
        "COMMISSION",
        "SEC_FEE",
        "STR_FEE",
        "R_FEE",
        "CDSC_FEE",
        "OPT_REG_FEE",
        "ADDITIONAL_FEE",
        "MISCELLANEOUS_FEE",
        "FUTURES_EXCHANGE_FEE",
        "LOW_PROCEEDS_COMMISSION",
        "BASE_CHARGE",
        "GENERAL_CHARGE",
        "GST_FEE",
        "TAF_FEE",
        "INDEX_OPTION_FEE",
        "UNKNOWN",
    }
)
SCHWAB_TRANSACTION_POSITION_EFFECTS = frozenset(
    {"OPENING", "CLOSING", "AUTOMATIC", "UNKNOWN"}
)
# Held securities are equities, ETFs, and mutual funds. The remaining documented quote asset
# families describe instruments Northstar deliberately cannot hold or value.
SCHWAB_QUOTE_ASSET_MAIN_TYPES = frozenset({"EQUITY", "MUTUAL_FUND"})
# Documented realtime (`NBBO`) and non-fee-liable delayed (`NFL`) quote contexts.
SCHWAB_QUOTE_CONTEXTS = frozenset({"NBBO", "NFL"})
SCHWAB_MARKET_SESSIONS = frozenset({"normal", "halted", "closed"})
SCHWAB_QUOTE_SECURITY_STATUSES = {
    "NORMAL": "normal",
    "HALTED": "halted",
    "CLOSED": "closed",
}


class SchwabValidationCategory(StrEnum):
    RESPONSE_ENVELOPE = "response_envelope"
    ACCOUNT_IDENTITY_MECHANICS = "account_identity_mechanics"
    # Retained for historical failed sync runs and the deliberately narrow synthetic fixture
    # contract. Production provider parsing emits one of the variant-specific categories below.
    BALANCE_STRUCTURE = "balance_structure"
    BALANCE_VARIANT_MALFORMED = "balance_variant_malformed"
    BALANCE_VARIANT_MISSING = "balance_variant_missing"
    POSITION_STRUCTURE = "position_structure"
    INSTRUMENT_STRUCTURE = "instrument_structure"
    INSTRUMENT_IDENTITY = "instrument_identity"
    INSTRUMENT_METADATA = "instrument_metadata"
    INSTRUMENT_SUBTYPE_MALFORMED = "instrument_subtype_malformed"
    INSTRUMENT_SUBTYPE_UNSUPPORTED = "instrument_subtype_unsupported"
    INSTRUMENT_TYPE_MALFORMED = "instrument_type_malformed"
    INSTRUMENT_TYPE_UNSUPPORTED = "instrument_type_unsupported"
    # Retained for historical failed sync runs. New parsing paths distinguish malformed values from
    # bounded but unsupported names.
    INSTRUMENT_TYPE = "instrument_type"
    # Retained in the allowlist for historical failed sync runs. New parsing paths emit one of the
    # narrower instrument categories above.
    INSTRUMENT_IDENTITY_TYPE = "instrument_identity_type"
    NUMERIC_VALUE = "numeric_value"
    UNSUPPORTED_BORROWING_SHORT_POSITION = "unsupported_borrowing_short_position"
    TRANSACTION_STRUCTURE = "transaction_structure"
    QUOTE_STRUCTURE = "quote_structure"
    QUOTE_SYMBOL_COVERAGE = "quote_symbol_coverage"
    QUOTE_ASSET_TYPE_MALFORMED = "quote_asset_type_malformed"
    QUOTE_ASSET_TYPE_UNSUPPORTED = "quote_asset_type_unsupported"
    QUOTE_CONTEXT = "quote_context"
    QUOTE_VARIANT_MISSING = "quote_variant_missing"
    QUOTE_VARIANT_MALFORMED = "quote_variant_malformed"
    QUOTE_SESSION_MALFORMED = "quote_session_malformed"
    QUOTE_SESSION_UNSUPPORTED = "quote_session_unsupported"
    QUOTE_OBSERVATION_TIME = "quote_observation_time"


@dataclass(frozen=True)
class SchwabTokenGrant:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    token_type: str
    expires_at: datetime
    refresh_expires_at: datetime | None
    scopes: tuple[str, ...]

    def __post_init__(self) -> None:
        _credential(self.access_token, "access token")
        _credential(self.refresh_token, "refresh token")
        if not self.token_type or len(self.token_type) > 64 or not self.token_type.isascii():
            raise ValueError("Schwab token type is malformed")
        if self.expires_at.tzinfo is None or (
            self.refresh_expires_at is not None and self.refresh_expires_at.tzinfo is None
        ):
            raise ValueError("Schwab token expiry must include a timezone")
        if any(
            not scope
            or len(scope) > 256
            or not scope.isascii()
            or any(character.isspace() for character in scope)
            for scope in self.scopes
        ):
            raise ValueError("Schwab token scope is malformed")


@dataclass(frozen=True)
class SchwabAccountReference:
    """An API-safe account reference that intentionally cannot carry a full account number."""

    provider_account_hash: str = field(repr=False)
    last_four: str | None = None

    def __post_init__(self) -> None:
        _provider_account_hash(self.provider_account_hash)
        if self.last_four is not None and (
            len(self.last_four) != 4 or not self.last_four.isascii() or not self.last_four.isalnum()
        ):
            raise ValueError("Schwab account suffix is malformed")


@dataclass(frozen=True)
class SchwabPosition:
    provider_instrument_key: str = field(repr=False)
    symbol: str | None = field(repr=False)
    description: str | None = field(repr=False)
    instrument_type: str = field(repr=False)
    quantity: Decimal = field(repr=False)
    market_value: Decimal | None = field(repr=False)
    average_price: Decimal | None = field(repr=False)

    def __post_init__(self) -> None:
        _provider_instrument_key(self.provider_instrument_key)
        _bounded_optional_text(self.symbol, "symbol", 32)
        _bounded_optional_text(self.description, "description", 512)
        if self.instrument_type not in {"equity", "etf", "mutual_fund", "bond", "cash", "bitcoin", "other"}:
            raise ValueError("Schwab instrument type is malformed")
        _decimal_value(self.quantity, DecimalKind.QUANTITY, "quantity")
        _decimal_value(self.market_value, DecimalKind.AMOUNT, "market value")
        _decimal_value(self.average_price, DecimalKind.PRICE, "average price")


@dataclass(frozen=True)
class SchwabAccountSnapshot:
    provider_account_hash: str = field(repr=False)
    observed_at_utc: datetime = field(repr=False)
    account_type: str | None = field(repr=False)
    liquidation_value: Decimal | None = field(repr=False)
    cash_balance: Decimal | None = field(repr=False)
    positions: tuple[SchwabPosition, ...] = field(repr=False)

    def __post_init__(self) -> None:
        _provider_account_hash(self.provider_account_hash)
        if self.observed_at_utc.tzinfo is None or len(self.positions) > MAX_SNAPSHOT_POSITIONS:
            raise ValueError("Schwab account snapshot is malformed")
        _bounded_optional_text(self.account_type, "account type", 64)
        _decimal_value(self.liquidation_value, DecimalKind.AMOUNT, "liquidation value")
        _decimal_value(self.cash_balance, DecimalKind.AMOUNT, "cash balance")
        if len({position.provider_instrument_key for position in self.positions}) != len(self.positions):
            raise ValueError("Schwab positions are duplicated")


@dataclass(frozen=True)
class SchwabTransaction:
    provider_transaction_key: str = field(repr=False)
    provider_account_hash: str = field(repr=False)
    provider_source_time_utc: datetime = field(repr=False)
    trade_date: datetime | None = field(repr=False)
    settlement_date: datetime | None = field(repr=False)
    imported_at_utc: datetime = field(repr=False)
    provider_type: str
    provider_status: str
    activity_type: str
    sub_account: str
    net_amount: Decimal = field(repr=False)
    items: tuple[SchwabTransactionItem, ...] = field(repr=False)

    def __post_init__(self) -> None:
        _provider_transaction_key(self.provider_transaction_key)
        _provider_account_hash(self.provider_account_hash)
        for value in (
            self.provider_source_time_utc,
            self.trade_date,
            self.settlement_date,
            self.imported_at_utc,
        ):
            if value is not None and value.tzinfo is None:
                raise ValueError("Schwab transaction date is malformed")
        if self.provider_type not in SCHWAB_TRANSACTION_TYPES:
            raise ValueError("Schwab transaction type is malformed")
        if self.provider_status not in SCHWAB_TRANSACTION_STATUSES:
            raise ValueError("Schwab transaction status is malformed")
        if self.activity_type not in SCHWAB_TRANSACTION_ACTIVITY_TYPES:
            raise ValueError("Schwab transaction activity is malformed")
        if self.sub_account not in {"CASH", "MARGIN"}:
            raise ValueError("Schwab transaction sub-account is malformed")
        canonical_decimal(self.net_amount, DecimalKind.AMOUNT)
        if len(self.items) > MAX_TRANSACTION_ITEMS:
            raise ValueError("Schwab transaction items are malformed")


@dataclass(frozen=True)
class SchwabTransactionItem:
    provider_instrument_key: str | None = field(repr=False)
    symbol: str | None = field(repr=False)
    instrument_type: str | None
    amount: Decimal | None = field(repr=False)
    cost: Decimal | None = field(repr=False)
    price: Decimal | None = field(repr=False)
    fee_type: str | None
    position_effect: str | None

    def __post_init__(self) -> None:
        if self.provider_instrument_key is not None:
            _provider_instrument_key(self.provider_instrument_key)
        _bounded_optional_text(self.symbol, "symbol", 32)
        if self.instrument_type not in {
            None,
            "equity",
            "etf",
            "mutual_fund",
            "bond",
            "cash",
        }:
            raise ValueError("Schwab transaction instrument type is malformed")
        for value, kind in (
            (self.amount, DecimalKind.QUANTITY),
            (self.cost, DecimalKind.AMOUNT),
            (self.price, DecimalKind.PRICE),
        ):
            if value is not None:
                canonical_decimal(value, kind, allow_negative=True)
        if self.fee_type is not None and self.fee_type not in SCHWAB_TRANSACTION_FEE_TYPES:
            raise ValueError("Schwab transaction fee type is malformed")
        if (
            self.position_effect is not None
            and self.position_effect not in SCHWAB_TRANSACTION_POSITION_EFFECTS
        ):
            raise ValueError("Schwab transaction position effect is malformed")


@dataclass(frozen=True)
class SchwabQuote:
    """One bounded provider price observation with its own provenance and session context.

    A quote deliberately cannot represent a zero, negative, or untimed price. Callers that
    cannot obtain a complete observation must preserve their last valid one instead of
    substituting a placeholder value.
    """

    symbol: str = field(repr=False)
    asset_main_type: str
    price: Decimal = field(repr=False)
    previous_close: Decimal = field(repr=False)
    observed_at_utc: datetime = field(repr=False)
    quote_context: str
    market_session: str

    def __post_init__(self) -> None:
        _quote_symbol(self.symbol)
        if self.asset_main_type not in SCHWAB_QUOTE_ASSET_MAIN_TYPES:
            raise ValueError("Schwab quote asset type is malformed")
        if self.quote_context not in SCHWAB_QUOTE_CONTEXTS:
            raise ValueError("Schwab quote context is malformed")
        if self.market_session not in SCHWAB_MARKET_SESSIONS:
            raise ValueError("Schwab quote market session is malformed")
        if self.observed_at_utc.tzinfo is None:
            raise ValueError("Schwab quote observation time is malformed")
        for value, label in (
            (self.price, "quote price"),
            (self.previous_close, "previous close"),
        ):
            if value <= 0:
                raise ValueError(f"Schwab {label} is malformed")
            canonical_decimal(value, DecimalKind.PRICE, allow_negative=False)


class SchwabOAuthClient(Protocol):
    """Narrow OAuth surface; implementations must not expose a generic HTTP request method."""

    def authorization_url(self, *, state: str) -> str: ...

    async def exchange_authorization_code(self, *, code: str) -> SchwabTokenGrant: ...

    async def refresh_access(self, *, refresh_token: str) -> SchwabTokenGrant: ...


class SchwabReadClient(Protocol):
    """Read-only provider surface. Trading and money movement are intentionally unrepresentable."""

    async def linked_accounts(self, *, access_token: str) -> tuple[SchwabAccountReference, ...]: ...

    async def account_snapshot(
        self, *, access_token: str, provider_account_hash: str
    ) -> SchwabAccountSnapshot: ...

    async def transactions(
        self,
        *,
        access_token: str,
        provider_account_hash: str,
        start_date: date,
        end_date: date,
    ) -> tuple[SchwabTransaction, ...]: ...

    async def quotes(
        self, *, access_token: str, symbols: tuple[str, ...]
    ) -> tuple[SchwabQuote, ...]: ...


class SchwabOAuthError(RuntimeError):
    def __init__(self, code: str) -> None:
        if code not in SCHWAB_OAUTH_ERROR_CODES:
            raise ValueError("Schwab OAuth error code is not allowed")
        super().__init__(code)
        self.code = code


class SchwabValidationError(SchwabOAuthError):
    """A closed, privacy-safe provider response validation failure."""

    def __init__(self, category: SchwabValidationCategory) -> None:
        self.category = category
        super().__init__(f"provider_validation_{category.value}")


class HttpSchwabOAuthClient:
    """Documented Schwab OAuth flow with no provider-generic request escape hatch."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = _credential(client_id, "client ID")
        self._client_secret = _credential(client_secret, "client secret")
        self._redirect_uri = redirect_uri
        self._http = httpx.AsyncClient(
            timeout=10,
            follow_redirects=False,
            transport=transport,
        )

    def authorization_url(self, *, state: str) -> str:
        state = _oauth_parameter(state, "state", 512)
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "state": state,
            }
        )
        return f"{SCHWAB_AUTHORIZE_URL}?{query}"

    async def exchange_authorization_code(self, *, code: str) -> SchwabTokenGrant:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": _oauth_parameter(code, "authorization code", 8_192),
                "redirect_uri": self._redirect_uri,
            }
        )

    async def refresh_access(self, *, refresh_token: str) -> SchwabTokenGrant:
        return await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": _credential(refresh_token, "refresh token"),
            }
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _token_request(self, form: dict[str, str]) -> SchwabTokenGrant:
        try:
            async with self._http.stream(
                "POST",
                SCHWAB_TOKEN_URL,
                data=form,
                auth=httpx.BasicAuth(self._client_id, self._client_secret),
                headers={"Accept": "application/json"},
            ) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_OAUTH_RESPONSE_BYTES:
                        raise SchwabOAuthError("provider_response_too_large")
                    content.extend(chunk)
        except httpx.TimeoutException:
            raise SchwabOAuthError("provider_timeout") from None
        except httpx.RequestError:
            raise SchwabOAuthError("provider_unavailable") from None
        if response.status_code != 200:
            raise SchwabOAuthError(_oauth_status_code(response.status_code))
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SchwabOAuthError("provider_response_invalid") from None
        return _token_grant(payload)


class HttpSchwabReadClient:
    """A deliberately small read client with no generic provider-request escape hatch."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            timeout=SCHWAB_READ_TIMEOUT_SECONDS,
            follow_redirects=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def linked_accounts(self, *, access_token: str) -> tuple[SchwabAccountReference, ...]:
        # This is the only trader endpoint reachable from this slice.
        try:
            async with self._http.stream(
                "GET",
                f"{SCHWAB_TRADER_BASE_URL}/accounts/accountNumbers",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {_credential(access_token, 'access token')}",
                },
            ) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_DISCOVERY_RESPONSE_BYTES:
                        raise SchwabOAuthError("provider_response_too_large")
                    content.extend(chunk)
        except httpx.TimeoutException:
            raise SchwabOAuthError("provider_timeout") from None
        except httpx.RequestError:
            raise SchwabOAuthError("provider_unavailable") from None
        if response.status_code != 200:
            raise SchwabOAuthError(_oauth_status_code(response.status_code))
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise SchwabOAuthError("provider_response_invalid")
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SchwabOAuthError("provider_response_invalid") from None
        finally:
            content.clear()
        return _account_references(payload)

    async def transactions(
        self,
        *,
        access_token: str,
        provider_account_hash: str,
        start_date: date,
        end_date: date,
    ) -> tuple[SchwabTransaction, ...]:
        """Read one bounded transaction window for an already-mapped opaque account hash."""
        try:
            provider_account_hash = _provider_account_hash(provider_account_hash)
        except ValueError:
            raise SchwabOAuthError("provider_response_invalid") from None
        if (
            not isinstance(start_date, date)
            or isinstance(start_date, datetime)
            or not isinstance(end_date, date)
            or isinstance(end_date, datetime)
            or start_date > end_date
            or end_date - start_date > MAX_TRANSACTION_RANGE
        ):
            raise ValueError("Schwab transaction date window is invalid")
        params = {
            "startDate": f"{start_date.isoformat()}T00:00:00.000Z",
            "endDate": f"{end_date.isoformat()}T23:59:59.999Z",
            "types": ",".join(SCHWAB_TRANSACTION_TYPES),
        }
        try:
            async with self._http.stream(
                "GET",
                f"{SCHWAB_TRADER_BASE_URL}/accounts/{quote(provider_account_hash, safe='')}/transactions",
                params=params,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {_credential(access_token, 'access token')}",
                },
            ) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_TRANSACTION_RESPONSE_BYTES:
                        raise SchwabOAuthError("provider_response_too_large")
                    content.extend(chunk)
        except httpx.TimeoutException:
            raise SchwabOAuthError("provider_timeout") from None
        except httpx.RequestError:
            raise SchwabOAuthError("provider_unavailable") from None
        if response.status_code != 200:
            status_code = _oauth_status_code(response.status_code)
            if status_code == "provider_response_invalid":
                raise SchwabValidationError(SchwabValidationCategory.RESPONSE_ENVELOPE)
            raise SchwabOAuthError(status_code)
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise SchwabValidationError(SchwabValidationCategory.RESPONSE_ENVELOPE)
        try:
            payload = json.loads(content, parse_float=Decimal, parse_int=Decimal)
            return _provider_transactions(
                payload,
                provider_account_hash=provider_account_hash,
                start_date=start_date,
                end_date=end_date,
                imported_at=datetime.now(UTC),
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SchwabValidationError(SchwabValidationCategory.RESPONSE_ENVELOPE) from None
        finally:
            content.clear()

    async def quotes(
        self, *, access_token: str, symbols: tuple[str, ...]
    ) -> tuple[SchwabQuote, ...]:
        """Read one bounded batch of quotes for symbols the caller already knows are held.

        The caller supplies the exact symbol set; this transport neither discovers symbols nor
        accepts a partial answer. Every requested symbol must be present exactly once, so a
        missing observation can never be mistaken for a zero price.
        """
        requested = _quote_symbols(symbols)
        params = {
            "symbols": ",".join(requested),
            "fields": "quote",
            "indicative": "false",
        }
        try:
            async with self._http.stream(
                "GET",
                f"{SCHWAB_MARKET_DATA_BASE_URL}/quotes",
                params=params,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {_credential(access_token, 'access token')}",
                },
            ) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_QUOTE_RESPONSE_BYTES:
                        raise SchwabOAuthError("provider_response_too_large")
                    content.extend(chunk)
        except httpx.TimeoutException:
            raise SchwabOAuthError("provider_timeout") from None
        except httpx.RequestError:
            raise SchwabOAuthError("provider_unavailable") from None
        if response.status_code != 200:
            status_code = _oauth_status_code(response.status_code)
            if status_code == "provider_response_invalid":
                raise SchwabValidationError(SchwabValidationCategory.RESPONSE_ENVELOPE)
            raise SchwabOAuthError(status_code)
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise SchwabValidationError(SchwabValidationCategory.RESPONSE_ENVELOPE)
        try:
            payload = json.loads(content, parse_float=Decimal, parse_int=Decimal)
            return _provider_quotes(
                payload,
                requested=requested,
                imported_at=datetime.now(UTC),
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SchwabValidationError(SchwabValidationCategory.RESPONSE_ENVELOPE) from None
        finally:
            content.clear()

    async def account_snapshot(
        self, *, access_token: str, provider_account_hash: str
    ) -> SchwabAccountSnapshot:
        """Read one already-mapped account with positions, retaining no raw response.

        This is intentionally the only balances-and-positions endpoint exposed by the client.
        The path value is the provider's opaque account hash, never a plain account number.
        """
        try:
            provider_account_hash = _provider_account_hash(provider_account_hash)
        except ValueError:
            raise SchwabOAuthError("provider_response_invalid") from None
        try:
            async with self._http.stream(
                "GET",
                f"{SCHWAB_TRADER_BASE_URL}/accounts/{quote(provider_account_hash, safe='')}",
                params={"fields": "positions"},
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {_credential(access_token, 'access token')}",
                },
            ) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_SNAPSHOT_RESPONSE_BYTES:
                        raise SchwabOAuthError("provider_response_too_large")
                    content.extend(chunk)
        except httpx.TimeoutException:
            raise SchwabOAuthError("provider_timeout") from None
        except httpx.RequestError:
            raise SchwabOAuthError("provider_unavailable") from None
        if response.status_code != 200:
            status_code = _oauth_status_code(response.status_code)
            if status_code == "provider_response_invalid":
                raise SchwabValidationError(SchwabValidationCategory.RESPONSE_ENVELOPE)
            raise SchwabOAuthError(status_code)
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise SchwabValidationError(SchwabValidationCategory.RESPONSE_ENVELOPE)
        try:
            # Decimal parsing avoids introducing binary floating-point into financial values.
            payload = json.loads(content, parse_float=Decimal, parse_int=Decimal)
            return _account_snapshot(payload, provider_account_hash=provider_account_hash)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SchwabValidationError(SchwabValidationCategory.RESPONSE_ENVELOPE) from None
        finally:
            content.clear()


def parse_synthetic_account_snapshot(payload: Any, *, provider_account_hash: str) -> SchwabAccountSnapshot:
    """Parse the deliberately narrow synthetic fixture contract used before live reads exist.

    The raw fixture is transient: callers receive only normalized domain values and must never
    persist ``payload``.  Production HTTP account/position requests are intentionally absent.
    """
    if not isinstance(payload, dict) or not {
        "accountHash",
        "observedAt",
        "accountType",
        "balances",
    }.issubset(payload):
        raise SchwabValidationError(SchwabValidationCategory.RESPONSE_ENVELOPE)
    if (
        payload["accountHash"] != provider_account_hash
        or not isinstance(payload["observedAt"], str)
        or len(payload["observedAt"]) > 64
    ):
        raise SchwabValidationError(SchwabValidationCategory.ACCOUNT_IDENTITY_MECHANICS)
    balances = payload["balances"]
    positions = payload.get("positions")
    if not isinstance(balances, dict) or not {
        "liquidationValue",
        "cashBalance",
    }.issubset(balances):
        raise SchwabValidationError(SchwabValidationCategory.BALANCE_STRUCTURE)
    if positions is None:
        positions = []
    if not isinstance(positions, list):
        raise SchwabValidationError(SchwabValidationCategory.POSITION_STRUCTURE)
    if len(positions) > MAX_SNAPSHOT_POSITIONS:
        raise SchwabValidationError(SchwabValidationCategory.POSITION_STRUCTURE)
    try:
        observed_at = datetime.fromisoformat(payload["observedAt"].replace("Z", "+00:00"))
        if observed_at.tzinfo is None:
            raise ValueError
        if observed_at > datetime.now(UTC) + MAX_OBSERVATION_FUTURE_SKEW:
            raise ValueError("future observation")
    except (TypeError, ValueError):
        raise SchwabValidationError(SchwabValidationCategory.RESPONSE_ENVELOPE) from None
    try:
        account_type = _required_text(payload["accountType"], "account type", 64)
    except (KeyError, TypeError, ValueError):
        raise SchwabValidationError(SchwabValidationCategory.ACCOUNT_IDENTITY_MECHANICS) from None
    parsed_positions = tuple(_synthetic_position(item) for item in positions)
    try:
        liquidation_value = _required_decimal(balances["liquidationValue"], DecimalKind.AMOUNT)
        cash_balance = _required_decimal(balances["cashBalance"], DecimalKind.AMOUNT)
        return SchwabAccountSnapshot(
            provider_account_hash=provider_account_hash,
            observed_at_utc=observed_at.astimezone(UTC),
            account_type=account_type,
            liquidation_value=liquidation_value,
            cash_balance=cash_balance,
            positions=parsed_positions,
        )
    except (KeyError, TypeError, ValueError):
        raise SchwabValidationError(SchwabValidationCategory.NUMERIC_VALUE) from None


def _synthetic_position(payload: Any) -> SchwabPosition:
    if not isinstance(payload, dict) or not {
        "instrument",
        "quantity",
        "marketValue",
        "averagePrice",
    }.issubset(payload):
        _validation(SchwabValidationCategory.POSITION_STRUCTURE)
    instrument = payload["instrument"]
    if not isinstance(instrument, dict):
        _validation(SchwabValidationCategory.INSTRUMENT_STRUCTURE)
    description = instrument.get("description")
    try:
        provider_key = _required_text(instrument["providerKey"], "instrument key", 512)
        symbol = _required_text(instrument["symbol"], "symbol", 32).upper()
    except (KeyError, TypeError, ValueError):
        _validation(SchwabValidationCategory.INSTRUMENT_IDENTITY)
    try:
        description = None if description is None else _required_text(description, "description", 512)
    except (TypeError, ValueError):
        _validation(SchwabValidationCategory.INSTRUMENT_METADATA)
    try:
        instrument_type = _required_text(instrument["type"], "instrument type", 32)
    except (KeyError, TypeError, ValueError):
        _validation(SchwabValidationCategory.INSTRUMENT_TYPE_MALFORMED)
    if instrument_type not in {
        "equity",
        "etf",
        "mutual_fund",
        "bond",
        "cash",
        "bitcoin",
        "other",
    }:
        _validation(SchwabValidationCategory.INSTRUMENT_TYPE_UNSUPPORTED)
    try:
        return SchwabPosition(
            provider_instrument_key=provider_key,
            symbol=symbol,
            description=description,
            instrument_type=instrument_type,
            quantity=_required_decimal(payload["quantity"], DecimalKind.QUANTITY),
            market_value=_required_decimal(payload["marketValue"], DecimalKind.AMOUNT),
            average_price=_required_decimal(payload["averagePrice"], DecimalKind.PRICE),
        )
    except (KeyError, TypeError, ValueError):
        _validation(SchwabValidationCategory.NUMERIC_VALUE)


def _account_snapshot(payload: Any, *, provider_account_hash: str) -> SchwabAccountSnapshot:
    """Normalize the documented single-account response without retaining its payload."""
    if not isinstance(payload, dict) or "securitiesAccount" not in payload:
        _validation(SchwabValidationCategory.RESPONSE_ENVELOPE)
    account = payload["securitiesAccount"]
    if not isinstance(account, dict):
        _validation(SchwabValidationCategory.RESPONSE_ENVELOPE)
    account_number = account.get("accountNumber")
    account_mechanics = account.get("type")
    balances = account.get("currentBalances")
    positions = account.get("positions")
    if (
        not isinstance(account_number, str)
        or not account_number.isascii()
        or not 4 <= len(account_number) <= 32
        or account_number != account_number.strip()
        or any(character.isspace() for character in account_number)
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in account_number)
        or not isinstance(account_mechanics, str)
    ):
        _validation(SchwabValidationCategory.ACCOUNT_IDENTITY_MECHANICS)
    if not isinstance(balances, dict):
        _validation(SchwabValidationCategory.BALANCE_VARIANT_MALFORMED)
    if positions is None:
        positions = []
    if not isinstance(positions, list) or len(positions) > MAX_SNAPSHOT_POSITIONS:
        _validation(SchwabValidationCategory.POSITION_STRUCTURE)
    try:
        parsed_positions = tuple(_provider_position(position) for position in positions)
        if len({item.provider_instrument_key for item in parsed_positions}) != len(parsed_positions):
            _validation(SchwabValidationCategory.POSITION_STRUCTURE)
        liquidation_value, cash_balance = _provider_balances(account_mechanics, balances)
        return SchwabAccountSnapshot(
            provider_account_hash=provider_account_hash,
            observed_at_utc=datetime.now(UTC),
            # CASH/MARGIN describes brokerage mechanics, not the account's tax registration.
            # The encrypted local mapping supplies the authoritative Northstar account type.
            account_type=None,
            liquidation_value=liquidation_value,
            cash_balance=cash_balance,
            positions=parsed_positions,
        )
    except SchwabValidationError:
        raise
    except (TypeError, ValueError):
        _validation(SchwabValidationCategory.NUMERIC_VALUE)


def _provider_position(payload: Any) -> SchwabPosition:
    if not isinstance(payload, dict):
        _validation(SchwabValidationCategory.POSITION_STRUCTURE)
    instrument = payload.get("instrument")
    if not isinstance(instrument, dict):
        _validation(SchwabValidationCategory.INSTRUMENT_STRUCTURE)
    short_value = payload.get("shortQuantity")
    short_quantity = (
        Decimal() if short_value is None else _provider_decimal(short_value, DecimalKind.QUANTITY)
    )
    if short_quantity != Decimal():
        # The published snapshot model deliberately does not represent short positions yet.
        _validation(SchwabValidationCategory.UNSUPPORTED_BORROWING_SHORT_POSITION)
    description = instrument.get("description")
    if description is not None:
        try:
            description = _required_text(description, "description", 512)
        except (TypeError, ValueError):
            _validation(SchwabValidationCategory.INSTRUMENT_METADATA)
    try:
        provider_key = _required_text(instrument.get("cusip"), "instrument key", 64)
        symbol = _required_text(instrument.get("symbol"), "symbol", 32).upper()
    except (TypeError, ValueError):
        _validation(SchwabValidationCategory.INSTRUMENT_IDENTITY)
    instrument_type = _provider_instrument_type(
        instrument.get("assetType"),
        collective_subtype=instrument.get("type"),
    )
    try:
        return SchwabPosition(
            provider_instrument_key=provider_key,
            symbol=symbol,
            description=description,
            instrument_type=instrument_type,
            quantity=_provider_decimal(payload.get("longQuantity"), DecimalKind.QUANTITY),
            market_value=_provider_decimal(payload.get("marketValue"), DecimalKind.AMOUNT),
            average_price=_provider_decimal(payload.get("averagePrice"), DecimalKind.PRICE),
        )
    except (TypeError, ValueError):
        _validation(SchwabValidationCategory.NUMERIC_VALUE)


def _provider_decimal(value: Any, kind: DecimalKind) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        _validation(SchwabValidationCategory.NUMERIC_VALUE)
    try:
        return Decimal(canonical_decimal(value, kind, allow_negative=False))
    except (TypeError, ValueError):
        _validation(SchwabValidationCategory.NUMERIC_VALUE)


def _provider_balances(mechanics: Any, balances: dict[str, Any]) -> tuple[Decimal, Decimal]:
    try:
        normalized = _required_text(mechanics, "account mechanics", 16).upper()
    except ValueError:
        _validation(SchwabValidationCategory.ACCOUNT_IDENTITY_MECHANICS)
    _reject_borrowing_balances(balances)
    if normalized == "CASH":
        return _required_balance_variant(_cash_component_balance_variant(balances))
    if normalized == "MARGIN":
        return _required_balance_variant(_margin_component_balance_variant(balances))
    _validation(SchwabValidationCategory.ACCOUNT_IDENTITY_MECHANICS)


def _cash_component_balance_variant(balances: dict[str, Any]) -> tuple[Decimal, Decimal] | None:
    fields = ("totalCash", "longNonMarginableMarketValue")
    if not any(field in balances for field in fields):
        return None
    if not all(field in balances for field in fields):
        _validation(SchwabValidationCategory.BALANCE_VARIANT_MALFORMED)
    cash = _provider_decimal(balances["totalCash"], DecimalKind.AMOUNT)
    securities = _provider_decimal(balances["longNonMarginableMarketValue"], DecimalKind.AMOUNT)
    return cash + securities, cash


def _margin_component_balance_variant(balances: dict[str, Any]) -> tuple[Decimal, Decimal] | None:
    fields = ("equity", "longMarginValue")
    if not any(field in balances for field in fields):
        return None
    if not all(field in balances for field in fields):
        _validation(SchwabValidationCategory.BALANCE_VARIANT_MALFORMED)
    equity = _provider_decimal(balances["equity"], DecimalKind.AMOUNT)
    securities = _provider_decimal(balances["longMarginValue"], DecimalKind.AMOUNT)
    cash = equity - securities
    if cash < 0:
        _validation(SchwabValidationCategory.UNSUPPORTED_BORROWING_SHORT_POSITION)
    return equity, cash


def _required_balance_variant(
    variant: tuple[Decimal, Decimal] | None,
) -> tuple[Decimal, Decimal]:
    if variant is None:
        _validation(SchwabValidationCategory.BALANCE_VARIANT_MISSING)
    return variant


def _reject_borrowing_balances(balances: dict[str, Any]) -> None:
    for field_name in ("marginBalance", "shortBalance", "shortMarginValue"):
        if field_name not in balances or balances[field_name] is None:
            continue
        value = _signed_provider_decimal(balances[field_name], DecimalKind.AMOUNT)
        if value != Decimal():
            _validation(SchwabValidationCategory.UNSUPPORTED_BORROWING_SHORT_POSITION)


def _signed_provider_decimal(value: Any, kind: DecimalKind) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        _validation(SchwabValidationCategory.NUMERIC_VALUE)
    try:
        return Decimal(canonical_decimal(value, kind, allow_negative=True))
    except (TypeError, ValueError):
        _validation(SchwabValidationCategory.NUMERIC_VALUE)


def _provider_transactions(
    payload: Any,
    *,
    provider_account_hash: str,
    start_date: date,
    end_date: date,
    imported_at: datetime,
) -> tuple[SchwabTransaction, ...]:
    if not isinstance(payload, list):
        _validation(SchwabValidationCategory.RESPONSE_ENVELOPE)
    if len(payload) >= MAX_TRANSACTIONS:
        raise SchwabOAuthError("provider_transaction_history_incomplete")
    records: list[SchwabTransaction] = []
    seen: set[str] = set()
    response_account_number: str | None = None
    for record in payload:
        if not isinstance(record, dict) or not {
            "activityId",
            "time",
            "accountNumber",
            "type",
            "status",
            "subAccount",
            "netAmount",
            "activityType",
        }.issubset(record):
            _validation(SchwabValidationCategory.TRANSACTION_STRUCTURE)
        activity_id = record["activityId"]
        account_number = record["accountNumber"]
        if (
            not isinstance(activity_id, Decimal)
            or activity_id != activity_id.to_integral_value()
            or not Decimal() <= activity_id <= Decimal(2**63 - 1)
            or not isinstance(account_number, str)
            or not account_number.isascii()
            or not 4 <= len(account_number) <= 32
            or account_number != account_number.strip()
            or any(character.isspace() for character in account_number)
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in account_number)
        ):
            _validation(SchwabValidationCategory.TRANSACTION_STRUCTURE)
        if response_account_number is None:
            response_account_number = account_number
        elif account_number != response_account_number:
            _validation(SchwabValidationCategory.TRANSACTION_STRUCTURE)
        provider_key = format(activity_id, "f")
        if provider_key in seen:
            _validation(SchwabValidationCategory.TRANSACTION_STRUCTURE)
        try:
            provider_type = _required_text(record["type"], "transaction type", 64)
            provider_status = _required_text(record["status"], "transaction status", 32)
            activity_type = _required_text(record["activityType"], "transaction activity", 64)
            sub_account = _required_text(record["subAccount"], "transaction sub-account", 32)
            source_time = _provider_datetime(record["time"], required=True)
            if (
                source_time is None
                or not start_date <= source_time.date() <= end_date
                or source_time > imported_at + MAX_OBSERVATION_FUTURE_SKEW
            ):
                raise ValueError("Schwab transaction source time is outside the requested window")
            trade_date = _provider_datetime(record.get("tradeDate"), required=False)
            settlement_date = _provider_datetime(record.get("settlementDate"), required=False)
            net_amount = _signed_provider_decimal(record["netAmount"], DecimalKind.AMOUNT)
            transfer_items = record.get("transferItems")
            if transfer_items is None:
                transfer_items = []
            if not isinstance(transfer_items, list) or len(transfer_items) > MAX_TRANSACTION_ITEMS:
                _validation(SchwabValidationCategory.TRANSACTION_STRUCTURE)
            items = tuple(_provider_transaction_item(item) for item in transfer_items)
            transaction = SchwabTransaction(
                provider_transaction_key=provider_key,
                provider_account_hash=provider_account_hash,
                provider_source_time_utc=source_time,
                trade_date=trade_date,
                settlement_date=settlement_date,
                imported_at_utc=imported_at,
                provider_type=provider_type,
                provider_status=provider_status,
                activity_type=activity_type,
                sub_account=sub_account,
                net_amount=net_amount,
                items=items,
            )
        except (KeyError, TypeError, ValueError):
            _validation(SchwabValidationCategory.TRANSACTION_STRUCTURE)
        records.append(transaction)
        seen.add(provider_key)
    return tuple(records)


def _provider_transaction_item(payload: Any) -> SchwabTransactionItem:
    if not isinstance(payload, dict):
        _validation(SchwabValidationCategory.TRANSACTION_STRUCTURE)
    instrument = payload.get("instrument")
    provider_key: str | None = None
    symbol: str | None = None
    instrument_type: str | None = None
    if instrument is not None:
        if not isinstance(instrument, dict):
            _validation(SchwabValidationCategory.INSTRUMENT_STRUCTURE)
        try:
            raw_key = instrument.get("cusip") or instrument.get("instrumentId")
            provider_key = _required_text(raw_key, "instrument key", 64)
            raw_symbol = instrument.get("symbol")
            symbol = None if raw_symbol is None else _required_text(raw_symbol, "symbol", 32).upper()
        except (TypeError, ValueError):
            _validation(SchwabValidationCategory.INSTRUMENT_IDENTITY)
        instrument_type = _provider_instrument_type(
            instrument.get("assetType"),
            collective_subtype=instrument.get("type"),
        )
    try:
        fee_type = _optional_enum(payload.get("feeType"), SCHWAB_TRANSACTION_FEE_TYPES, 64)
        position_effect = _optional_enum(
            payload.get("positionEffect"), SCHWAB_TRANSACTION_POSITION_EFFECTS, 32
        )
        return SchwabTransactionItem(
            provider_instrument_key=provider_key,
            symbol=symbol,
            instrument_type=instrument_type,
            amount=_optional_provider_decimal(payload.get("amount"), DecimalKind.QUANTITY),
            cost=_optional_provider_decimal(payload.get("cost"), DecimalKind.AMOUNT),
            price=_optional_provider_decimal(
                payload.get("price"), DecimalKind.PRICE, allow_negative=False
            ),
            fee_type=fee_type,
            position_effect=position_effect,
        )
    except (TypeError, ValueError):
        _validation(SchwabValidationCategory.TRANSACTION_STRUCTURE)


# Each supported quote family documents its own price members and its own observation
# timestamp. Undocumented aliases are deliberately not accepted as an alternate price.
_QUOTE_VARIANTS: dict[str, tuple[tuple[str, str], str]] = {
    "EQUITY": (("lastPrice", "closePrice"), "quoteTime"),
    "MUTUAL_FUND": (("nAV", "closePrice"), "tradeTime"),
}


def _quote_symbol(value: Any) -> str:
    normalized = _required_text(value, "quote symbol", MAX_QUOTE_SYMBOL_LENGTH)
    if normalized != normalized.upper() or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-/" for character in normalized
    ):
        raise ValueError("Schwab quote symbol is malformed")
    return normalized


def _quote_symbols(symbols: Any) -> tuple[str, ...]:
    """Validate the requested batch before any network activity."""
    if not isinstance(symbols, tuple) or not 1 <= len(symbols) <= MAX_QUOTE_SYMBOLS:
        raise ValueError("Schwab quote symbol batch is invalid")
    try:
        requested = tuple(_quote_symbol(symbol) for symbol in symbols)
    except (TypeError, ValueError):
        raise ValueError("Schwab quote symbol batch is invalid") from None
    if len(set(requested)) != len(requested):
        raise ValueError("Schwab quote symbol batch is invalid")
    return requested


def _provider_quotes(
    payload: Any,
    *,
    requested: tuple[str, ...],
    imported_at: datetime,
) -> tuple[SchwabQuote, ...]:
    if not isinstance(payload, dict):
        _validation(SchwabValidationCategory.RESPONSE_ENVELOPE)
    errors = payload.get("errors")
    if errors is not None and errors != {} and errors != []:
        # The provider reported at least one symbol it could not quote. The category never
        # carries the symbol itself.
        raise SchwabOAuthError("provider_quote_symbol_unavailable")
    returned = frozenset(key for key in payload if key != "errors")
    if returned != frozenset(requested):
        _validation(SchwabValidationCategory.QUOTE_SYMBOL_COVERAGE)
    return tuple(
        _provider_quote(payload[symbol], symbol=symbol, imported_at=imported_at)
        for symbol in requested
    )


def _provider_quote(payload: Any, *, symbol: str, imported_at: datetime) -> SchwabQuote:
    if not isinstance(payload, dict):
        _validation(SchwabValidationCategory.QUOTE_STRUCTURE)
    if payload.get("symbol") != symbol:
        _validation(SchwabValidationCategory.QUOTE_SYMBOL_COVERAGE)
    quote = payload.get("quote")
    if not isinstance(quote, dict):
        _validation(SchwabValidationCategory.QUOTE_STRUCTURE)
    asset_main_type = _quote_asset_main_type(payload.get("assetMainType"))
    quote_context = _quote_context(payload.get("quoteType"))
    price, previous_close = _quote_price_variant(quote, asset_main_type)
    observed_at = _quote_observed_at(quote, asset_main_type, imported_at=imported_at)
    market_session = _quote_market_session(quote)
    try:
        return SchwabQuote(
            symbol=symbol,
            asset_main_type=asset_main_type,
            price=price,
            previous_close=previous_close,
            observed_at_utc=observed_at,
            quote_context=quote_context,
            market_session=market_session,
        )
    except (TypeError, ValueError):
        _validation(SchwabValidationCategory.QUOTE_VARIANT_MALFORMED)


def _quote_asset_main_type(value: Any) -> str:
    try:
        normalized = _required_text(value, "quote asset type", 64)
    except (TypeError, ValueError):
        _validation(SchwabValidationCategory.QUOTE_ASSET_TYPE_MALFORMED)
    if normalized != normalized.upper() or any(
        not part or not part.isalpha() for part in normalized.split("_")
    ):
        _validation(SchwabValidationCategory.QUOTE_ASSET_TYPE_MALFORMED)
    if normalized not in SCHWAB_QUOTE_ASSET_MAIN_TYPES:
        _validation(SchwabValidationCategory.QUOTE_ASSET_TYPE_UNSUPPORTED)
    return normalized


def _quote_context(value: Any) -> str:
    try:
        normalized = _required_text(value, "quote context", 32)
    except (TypeError, ValueError):
        _validation(SchwabValidationCategory.QUOTE_CONTEXT)
    if normalized not in SCHWAB_QUOTE_CONTEXTS:
        _validation(SchwabValidationCategory.QUOTE_CONTEXT)
    return normalized


def _quote_price_variant(quote: dict[str, Any], asset_main_type: str) -> tuple[Decimal, Decimal]:
    names, _ = _QUOTE_VARIANTS[asset_main_type]
    if not any(name in quote for name in names):
        _validation(SchwabValidationCategory.QUOTE_VARIANT_MISSING)
    if not all(name in quote for name in names):
        _validation(SchwabValidationCategory.QUOTE_VARIANT_MALFORMED)
    latest, previous = names
    return _quote_decimal(quote[latest]), _quote_decimal(quote[previous])


def _quote_decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        _validation(SchwabValidationCategory.QUOTE_VARIANT_MALFORMED)
    try:
        price = Decimal(canonical_decimal(value, DecimalKind.PRICE, allow_negative=False))
    except (TypeError, ValueError):
        _validation(SchwabValidationCategory.QUOTE_VARIANT_MALFORMED)
    if price <= 0:
        # A zero or absent price must never become a valuation input.
        _validation(SchwabValidationCategory.QUOTE_VARIANT_MALFORMED)
    return price


def _quote_observed_at(
    quote: dict[str, Any], asset_main_type: str, *, imported_at: datetime
) -> datetime:
    _, name = _QUOTE_VARIANTS[asset_main_type]
    if name not in quote:
        _validation(SchwabValidationCategory.QUOTE_VARIANT_MISSING)
    return _provider_epoch_millis(quote[name], imported_at=imported_at)


def _provider_epoch_millis(value: Any, *, imported_at: datetime) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        _validation(SchwabValidationCategory.QUOTE_OBSERVATION_TIME)
    epoch = Decimal(value)
    if epoch != epoch.to_integral_value():
        _validation(SchwabValidationCategory.QUOTE_OBSERVATION_TIME)
    try:
        observed = datetime.fromtimestamp(int(epoch) / 1000, tz=UTC)
    except (OSError, OverflowError, ValueError):
        _validation(SchwabValidationCategory.QUOTE_OBSERVATION_TIME)
    if (
        observed < MIN_OBSERVATION_TIME
        or observed > imported_at + MAX_OBSERVATION_FUTURE_SKEW
    ):
        _validation(SchwabValidationCategory.QUOTE_OBSERVATION_TIME)
    return observed


def _quote_market_session(quote: dict[str, Any]) -> str:
    if "securityStatus" not in quote:
        _validation(SchwabValidationCategory.QUOTE_VARIANT_MISSING)
    try:
        normalized = _required_text(quote["securityStatus"], "quote session", 32).upper()
    except (TypeError, ValueError):
        _validation(SchwabValidationCategory.QUOTE_SESSION_MALFORMED)
    if normalized not in SCHWAB_QUOTE_SECURITY_STATUSES:
        _validation(SchwabValidationCategory.QUOTE_SESSION_UNSUPPORTED)
    return SCHWAB_QUOTE_SECURITY_STATUSES[normalized]


def _optional_provider_decimal(
    value: Any,
    kind: DecimalKind,
    *,
    allow_negative: bool = True,
) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise ValueError("Schwab provider decimal is malformed")
    return Decimal(canonical_decimal(value, kind, allow_negative=allow_negative))


def _optional_enum(value: Any, allowed: frozenset[str], maximum: int) -> str | None:
    if value is None:
        return None
    normalized = _required_text(value, "transaction enum", maximum)
    if normalized not in allowed:
        raise ValueError("Schwab transaction enum is malformed")
    return normalized


def _provider_datetime(value: Any, *, required: bool) -> datetime | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("Schwab provider date is malformed")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Schwab provider date is malformed")
    return parsed.astimezone(UTC)


def _provider_instrument_type(value: Any, *, collective_subtype: Any) -> str:
    try:
        normalized = _required_text(value, "instrument type", 64)
    except (TypeError, ValueError):
        _validation(SchwabValidationCategory.INSTRUMENT_TYPE_MALFORMED)
    if normalized != normalized.upper() or any(
        not part or not part.isalpha() for part in normalized.split("_")
    ):
        _validation(SchwabValidationCategory.INSTRUMENT_TYPE_MALFORMED)
    instrument_types = {
        "EQUITY": "equity",
        "MUTUAL_FUND": "mutual_fund",
        "FIXED_INCOME": "bond",
        "CASH_EQUIVALENT": "cash",
    }
    if normalized == "COLLECTIVE_INVESTMENT":
        return _provider_collective_investment_type(collective_subtype)
    if normalized not in instrument_types:
        _validation(SchwabValidationCategory.INSTRUMENT_TYPE_UNSUPPORTED)
    return instrument_types[normalized]


def _provider_collective_investment_type(value: Any) -> str:
    try:
        normalized = _required_text(value, "collective investment subtype", 64)
    except (TypeError, ValueError):
        _validation(SchwabValidationCategory.INSTRUMENT_SUBTYPE_MALFORMED)
    if normalized != normalized.upper() or any(
        not part or not part.isalpha() for part in normalized.split("_")
    ):
        _validation(SchwabValidationCategory.INSTRUMENT_SUBTYPE_MALFORMED)
    if normalized != "EXCHANGE_TRADED_FUND":
        _validation(SchwabValidationCategory.INSTRUMENT_SUBTYPE_UNSUPPORTED)
    return "etf"


def _validation(category: SchwabValidationCategory) -> NoReturn:
    raise SchwabValidationError(category)


def _required_text(value: Any, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or not value.isascii()
    ):
        raise ValueError(f"Schwab {label} is malformed")
    return value


def _required_decimal(value: Any, kind: DecimalKind) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("Schwab financial value is malformed")
    return Decimal(canonical_decimal(value, kind, allow_negative=False))


def _decimal_value(value: Decimal | None, kind: DecimalKind, label: str) -> None:
    if value is None or value < 0:
        raise ValueError(f"Schwab {label} is malformed")
    canonical_decimal(value, kind, allow_negative=False)


def _bounded_optional_text(value: str | None, label: str, maximum: int) -> None:
    if value is not None:
        _required_text(value, label, maximum)


def _provider_instrument_key(value: str) -> str:
    return _required_text(value, "instrument key", 512)


def _provider_transaction_key(value: str) -> str:
    if not value.isascii() or not value.isdigit() or not 1 <= len(value) <= 32:
        raise ValueError("Schwab transaction key is malformed")
    return value


def _credential(value: str, label: str) -> str:
    if (
        not value
        or len(value) > 8_192
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(f"Schwab {label} is malformed")
    return value


def _oauth_parameter(value: str, label: str, maximum_length: int) -> str:
    if (
        not value
        or len(value) > maximum_length
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(f"Schwab {label} is malformed")
    return value


def _oauth_status_code(status_code: int) -> str:
    if status_code in {400, 401, 403}:
        return "provider_authorization_rejected"
    if status_code == 429:
        return "provider_rate_limited"
    if status_code in {500, 502, 503, 504}:
        return "provider_unavailable"
    return "provider_response_invalid"


def _account_references(payload: Any) -> tuple[SchwabAccountReference, ...]:
    if not isinstance(payload, list) or len(payload) > MAX_DISCOVERED_ACCOUNTS:
        raise SchwabOAuthError("provider_response_invalid")
    references: list[SchwabAccountReference] = []
    seen_hashes: set[str] = set()
    seen_numbers: set[str] = set()
    for record in payload:
        if not isinstance(record, dict) or set(record) != {"accountNumber", "hashValue"}:
            raise SchwabOAuthError("provider_response_invalid")
        account_number = record["accountNumber"]
        account_hash = record["hashValue"]
        if (
            not isinstance(account_number, str)
            or not account_number.isascii()
            or not account_number.isdigit()
            or not 4 <= len(account_number) <= 32
            or not isinstance(account_hash, str)
            or account_hash in seen_hashes
            or account_number in seen_numbers
        ):
            raise SchwabOAuthError("provider_response_invalid")
        try:
            reference = SchwabAccountReference(account_hash, account_number[-4:])
        except ValueError:
            raise SchwabOAuthError("provider_response_invalid") from None
        references.append(reference)
        seen_hashes.add(account_hash)
        seen_numbers.add(account_number)
    return tuple(references)


def _token_grant(payload: Any) -> SchwabTokenGrant:
    if not isinstance(payload, dict):
        raise SchwabOAuthError("provider_response_invalid")
    try:
        access_token = payload["access_token"]
        refresh_token = payload["refresh_token"]
        token_type = payload["token_type"]
        expires_in = payload["expires_in"]
        scope = payload["scope"]
    except KeyError as exc:
        raise SchwabOAuthError("provider_response_invalid") from exc
    if (
        not isinstance(access_token, str)
        or not isinstance(refresh_token, str)
        or not isinstance(token_type, str)
        or not isinstance(expires_in, int)
        or isinstance(expires_in, bool)
        or not 1 <= expires_in <= 86_400
        or not isinstance(scope, str)
    ):
        raise SchwabOAuthError("provider_response_invalid")
    scopes = tuple(part for part in scope.split() if part)
    if token_type != BEARER_TOKEN_TYPE or scopes != ("api",):
        raise SchwabOAuthError("provider_response_invalid")
    now = datetime.now(UTC)
    try:
        return SchwabTokenGrant(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=token_type,
            expires_at=now + timedelta(seconds=expires_in),
            refresh_expires_at=now + REFRESH_TOKEN_LIFETIME,
            scopes=scopes,
        )
    except ValueError:
        raise SchwabOAuthError("provider_response_invalid") from None


def _provider_account_hash(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 512
        or normalized.isdigit()
        or any(character.isspace() for character in normalized)
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized)
    ):
        raise ValueError("Schwab provider account hash is malformed")
    return normalized
