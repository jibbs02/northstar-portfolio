from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

from northstar.domain import DecimalKind, canonical_decimal

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5_000
MAX_FIELD_CHARACTERS = 500
MARKET_VALUE_TOLERANCE = Decimal("0.01")

POSITION_HEADERS = (
    "Row Type",
    "Account Number",
    "Symbol",
    "Description",
    "Security Type",
    "Quantity",
    "Price",
    "Market Value",
    "Cost Basis",
    "As Of Date",
)
TRANSACTION_HEADERS = (
    "Account Number",
    "Trade Date",
    "Settlement Date",
    "Action",
    "Symbol",
    "Description",
    "Quantity",
    "Price",
    "Fees",
    "Amount",
    "Cost Basis",
)

TRANSACTION_TYPES = {
    "BUY": "buy",
    "SELL": "sell",
    "DIVIDEND": "dividend",
    "REINVESTMENT": "reinvestment",
    "INTEREST": "interest",
    "DEPOSIT": "deposit",
    "WITHDRAWAL": "withdrawal",
    "TRANSFER IN": "transfer_in",
    "TRANSFER OUT": "transfer_out",
    "FEE": "fee",
    "ADJUSTMENT": "adjustment",
}
INSTRUMENT_TYPES = {
    "EQUITY": "equity",
    "ETF": "etf",
    "MUTUAL FUND": "mutual_fund",
    "BOND": "bond",
    "CASH": "cash",
    "BITCOIN": "bitcoin",
    "OTHER": "other",
}


@dataclass(frozen=True)
class ValidationIssue:
    row: int | None
    field: str | None
    code: str


class ImportValidationError(ValueError):
    def __init__(self, issues: list[ValidationIssue]) -> None:
        super().__init__("Import validation failed")
        self.issues = issues


@dataclass(frozen=True)
class ImportPreview:
    adapter_key: str
    records: list[dict[str, Any]]
    account_identifiers: frozenset[str]
    reconciliation_status: str
    source_total: str | None
    calculated_total: str | None


class ImportAdapter(Protocol):
    key: str

    def parse(self, payload: bytes) -> ImportPreview: ...


def adapter_for(key: str) -> ImportAdapter:
    adapters: dict[str, ImportAdapter] = {
        ManualPositionsAdapter.key: ManualPositionsAdapter(),
        ManualTransactionsAdapter.key: ManualTransactionsAdapter(),
        # Retained solely so existing synthetic fixtures can exercise the same strict parser.
        # These values are not exposed by the production UI.
        "schwab_positions_synthetic_v1": ManualPositionsAdapter("schwab_positions_synthetic_v1"),
        "schwab_transactions_synthetic_v1": ManualTransactionsAdapter("schwab_transactions_synthetic_v1"),
    }
    try:
        return adapters[key]
    except KeyError as exc:
        raise ImportValidationError([ValidationIssue(None, None, "unsupported_adapter")]) from exc


class ManualPositionsAdapter:
    key = "manual_positions_csv_v1"

    def __init__(self, key: str | None = None) -> None:
        self.key = key or self.key

    def parse(self, payload: bytes) -> ImportPreview:
        rows = _read_rows(payload, POSITION_HEADERS)
        records: list[dict[str, Any]] = []
        identifiers: set[str] = set()
        issues: list[ValidationIssue] = []
        supplied_total: str | None = None
        for row_number, row in rows:
            row_type = _safe_text(row["Row Type"], row_number, "Row Type", issues).upper()
            account = _safe_text(row["Account Number"], row_number, "Account Number", issues, required=True)
            if account:
                identifiers.add(account)
            if row_type == "TOTAL":
                if supplied_total is not None:
                    issues.append(ValidationIssue(row_number, "Row Type", "duplicate_total"))
                supplied_total = _decimal(
                    row["Market Value"], DecimalKind.AMOUNT, row_number, "Market Value", issues
                )
                continue
            if row_type != "POSITION":
                issues.append(ValidationIssue(row_number, "Row Type", "unsupported_row_type"))
                continue
            symbol = _safe_text(row["Symbol"], row_number, "Symbol", issues, required=True)
            description = _safe_text(row["Description"], row_number, "Description", issues)
            instrument = INSTRUMENT_TYPES.get(
                _safe_text(row["Security Type"], row_number, "Security Type", issues, required=True).upper()
            )
            if instrument is None:
                issues.append(ValidationIssue(row_number, "Security Type", "unsupported_instrument"))
            source_date = _date(row["As Of Date"], row_number, "As Of Date", issues, required=True)
            quantity = _decimal(
                row["Quantity"],
                DecimalKind.QUANTITY,
                row_number,
                "Quantity",
                issues,
                allow_negative=False,
            )
            price = _decimal(
                row["Price"],
                DecimalKind.PRICE,
                row_number,
                "Price",
                issues,
                allow_negative=False,
            )
            market_value = _optional_decimal(
                row["Market Value"],
                DecimalKind.AMOUNT,
                row_number,
                "Market Value",
                issues,
                allow_negative=False,
            )
            if quantity is not None and price is not None and market_value is not None:
                calculated_value = Decimal(
                    canonical_decimal(Decimal(quantity) * Decimal(price), DecimalKind.AMOUNT)
                )
                if abs(Decimal(market_value) - calculated_value) > MARKET_VALUE_TOLERANCE:
                    issues.append(ValidationIssue(row_number, "Market Value", "market_value_price_mismatch"))
            records.append(
                {
                    "kind": "position",
                    "source_row": row_number,
                    "account_identifier": account,
                    "symbol": symbol,
                    "description": description or None,
                    "instrument_type": instrument,
                    "quantity": quantity,
                    "price": price,
                    "market_value": market_value,
                    "cost_basis": _optional_decimal(
                        row["Cost Basis"],
                        DecimalKind.AMOUNT,
                        row_number,
                        "Cost Basis",
                        issues,
                        allow_negative=False,
                    ),
                    "source_date": source_date,
                }
            )
        calculated_total = canonical_decimal(
            sum(
                (Decimal(record["market_value"]) for record in records if record["market_value"]),
                Decimal(0),
            ),
            DecimalKind.AMOUNT,
        )
        status = "not_provided"
        if supplied_total is not None:
            status = "matched" if supplied_total == calculated_total else "mismatched"
            if status == "mismatched":
                issues.append(ValidationIssue(None, "Market Value", "reconciliation_mismatch"))
        if len({record["source_date"] for record in records if record["source_date"] is not None}) > 1:
            issues.append(ValidationIssue(None, "As Of Date", "multiple_snapshot_dates"))
        symbols = [str(record["symbol"]).strip().upper() for record in records]
        if len(symbols) != len(set(symbols)):
            issues.append(ValidationIssue(None, "Symbol", "duplicate_security"))
        _finish_validation(records, identifiers, issues)
        return ImportPreview(
            self.key,
            records,
            frozenset(identifiers),
            status,
            supplied_total,
            calculated_total,
        )


class ManualTransactionsAdapter:
    key = "manual_transactions_csv_v1"

    def __init__(self, key: str | None = None) -> None:
        self.key = key or self.key

    def parse(self, payload: bytes) -> ImportPreview:
        rows = _read_rows(payload, TRANSACTION_HEADERS)
        records: list[dict[str, Any]] = []
        identifiers: set[str] = set()
        issues: list[ValidationIssue] = []
        supplied_total: str | None = None
        for row_number, row in rows:
            account = _safe_text(row["Account Number"], row_number, "Account Number", issues, required=True)
            if account:
                identifiers.add(account)
            action = _safe_text(row["Action"], row_number, "Action", issues, required=True).upper()
            if action == "TOTAL":
                if supplied_total is not None:
                    issues.append(ValidationIssue(row_number, "Action", "duplicate_total"))
                supplied_total = _decimal(row["Amount"], DecimalKind.AMOUNT, row_number, "Amount", issues)
                continue
            transaction_type = TRANSACTION_TYPES.get(action)
            if transaction_type is None:
                issues.append(ValidationIssue(row_number, "Action", "unsupported_transaction_type"))
            symbol = _safe_text(row["Symbol"], row_number, "Symbol", issues)
            description = _safe_text(row["Description"], row_number, "Description", issues)
            trade_date = _date(row["Trade Date"], row_number, "Trade Date", issues, required=True)
            settlement_date = _date(
                row["Settlement Date"],
                row_number,
                "Settlement Date",
                issues,
                required=False,
                allow_future=True,
            )
            quantity = _optional_decimal(
                row["Quantity"],
                DecimalKind.QUANTITY,
                row_number,
                "Quantity",
                issues,
                allow_negative=False,
            )
            price = _optional_decimal(
                row["Price"],
                DecimalKind.PRICE,
                row_number,
                "Price",
                issues,
                allow_negative=False,
            )
            amount = _optional_decimal(row["Amount"], DecimalKind.AMOUNT, row_number, "Amount", issues)
            fees = _optional_decimal(
                row["Fees"],
                DecimalKind.AMOUNT,
                row_number,
                "Fees",
                issues,
                allow_negative=False,
            )
            cost_basis = _optional_decimal(
                row["Cost Basis"],
                DecimalKind.AMOUNT,
                row_number,
                "Cost Basis",
                issues,
                allow_negative=False,
            )
            if transaction_type in {"buy", "sell", "reinvestment"}:
                if not symbol:
                    issues.append(ValidationIssue(row_number, "Symbol", "required"))
                if quantity is None:
                    issues.append(ValidationIssue(row_number, "Quantity", "required"))
                if price is None:
                    issues.append(ValidationIssue(row_number, "Price", "required"))
            if (
                transaction_type
                in {
                    "dividend",
                    "interest",
                    "deposit",
                    "withdrawal",
                    "transfer_in",
                    "transfer_out",
                    "fee",
                    "adjustment",
                }
                and amount is None
            ):
                issues.append(ValidationIssue(row_number, "Amount", "required"))
            records.append(
                {
                    "kind": "transaction",
                    "source_row": row_number,
                    "account_identifier": account,
                    "transaction_type": transaction_type,
                    "trade_date": trade_date,
                    "settlement_date": settlement_date,
                    "symbol": symbol or None,
                    "description": description or None,
                    "quantity": quantity,
                    "price": price,
                    "fees": fees,
                    "amount": amount,
                    "cost_basis": cost_basis,
                }
            )
        calculated_total = canonical_decimal(
            sum((Decimal(record["amount"]) for record in records if record["amount"]), Decimal(0)),
            DecimalKind.AMOUNT,
        )
        status = "not_provided"
        if supplied_total is not None:
            status = "matched" if supplied_total == calculated_total else "mismatched"
            if status == "mismatched":
                issues.append(ValidationIssue(None, "Amount", "reconciliation_mismatch"))
        _finish_validation(records, identifiers, issues)
        return ImportPreview(
            self.key,
            records,
            frozenset(identifiers),
            status,
            supplied_total,
            calculated_total,
        )


def _read_rows(payload: bytes, expected_headers: tuple[str, ...]) -> list[tuple[int, dict[str, str]]]:
    if not payload:
        raise ImportValidationError([ValidationIssue(None, None, "empty_file")])
    if len(payload) > MAX_FILE_BYTES:
        raise ImportValidationError([ValidationIssue(None, None, "file_too_large")])
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportValidationError([ValidationIssue(None, None, "invalid_encoding")]) from exc
    if "\x00" in text:
        raise ImportValidationError([ValidationIssue(None, None, "invalid_encoding")])
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(reader, None)
        if header != list(expected_headers) or len(set(header or [])) != len(expected_headers):
            raise ImportValidationError([ValidationIssue(1, None, "invalid_headers")])
        rows: list[tuple[int, dict[str, str]]] = []
        for row_number, values in enumerate(reader, start=2):
            if row_number - 1 > MAX_ROWS:
                raise ImportValidationError([ValidationIssue(None, None, "too_many_rows")])
            if len(values) != len(expected_headers):
                raise ImportValidationError([ValidationIssue(row_number, None, "inconsistent_columns")])
            if any(len(value) > MAX_FIELD_CHARACTERS for value in values):
                raise ImportValidationError([ValidationIssue(row_number, None, "field_too_long")])
            if not any(value.strip() for value in values):
                continue
            rows.append((row_number, dict(zip(expected_headers, values, strict=True))))
    except csv.Error as exc:
        raise ImportValidationError([ValidationIssue(None, None, "malformed_csv")]) from exc
    if not rows:
        raise ImportValidationError([ValidationIssue(None, None, "no_data_rows")])
    return rows


def _safe_text(
    value: str,
    row: int,
    field: str,
    issues: list[ValidationIssue],
    *,
    required: bool = False,
) -> str:
    cleaned = value.strip()
    if required and not cleaned:
        issues.append(ValidationIssue(row, field, "required"))
    if cleaned.startswith(("=", "+", "-", "@")):
        issues.append(ValidationIssue(row, field, "formula_injection"))
        return ""
    return cleaned


def _decimal(
    value: str,
    kind: DecimalKind,
    row: int,
    field: str,
    issues: list[ValidationIssue],
    *,
    allow_negative: bool = True,
) -> str | None:
    if not value.strip():
        issues.append(ValidationIssue(row, field, "required"))
        return None
    return _optional_decimal(value, kind, row, field, issues, allow_negative=allow_negative)


def _optional_decimal(
    value: str,
    kind: DecimalKind,
    row: int,
    field: str,
    issues: list[ValidationIssue],
    *,
    allow_negative: bool = True,
) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return canonical_decimal(cleaned, kind, allow_negative=allow_negative)
    except (TypeError, ValueError):
        issues.append(ValidationIssue(row, field, "invalid_decimal"))
        return None


def _date(
    value: str,
    row: int,
    field: str,
    issues: list[ValidationIssue],
    *,
    required: bool,
    allow_future: bool = False,
) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        if required:
            issues.append(ValidationIssue(row, field, "required"))
        return None
    try:
        parsed = date.fromisoformat(cleaned)
        if not allow_future and parsed > datetime.now(UTC).date():
            issues.append(ValidationIssue(row, field, "future_date"))
            return None
        return parsed.isoformat()
    except ValueError:
        issues.append(ValidationIssue(row, field, "invalid_date"))
        return None


def _finish_validation(
    records: list[dict[str, Any]], identifiers: set[str], issues: list[ValidationIssue]
) -> None:
    if len(identifiers) != 1:
        issues.append(ValidationIssue(None, "Account Number", "ambiguous_account"))
    if not records:
        issues.append(ValidationIssue(None, None, "no_importable_rows"))
    if issues:
        raise ImportValidationError(issues)
