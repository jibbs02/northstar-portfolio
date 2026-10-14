from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from northstar.domain import (
    AllocationHolding,
    EnvelopeFact,
    HoldingFact,
    TransactionFact,
    ValuationPoint,
    allocate_inflow,
    bitcoin_guardrail,
    flow_summary,
    performance_summary,
    portfolio_summary,
    reconcile_envelopes,
)

NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)


def holding(
    *,
    account_id: str = "account-a",
    account_name: str = "Invented Roth",
    security_id: str = "security-a",
    symbol: str = "SYNA",
    asset_class: str = "equity",
    quantity: str = "10",
    price: str | None = "10",
    snapshot_value: str | None = None,
    basis: str | None = "80",
    target: str | None = "60",
    eligible: bool = True,
    position_age_hours: int = 1,
    price_age_hours: int = 1,
) -> HoldingFact:
    return HoldingFact(
        account_id,
        account_name,
        "roth_ira",
        "tax_free",
        security_id,
        symbol,
        f"{symbol} synthetic holding",
        asset_class,
        Decimal(quantity),
        Decimal(snapshot_value) if snapshot_value is not None else None,
        Decimal(basis) if basis is not None else None,
        NOW - timedelta(hours=position_age_hours),
        Decimal(price) if price is not None else None,
        NOW - timedelta(hours=price_age_hours) if price is not None else None,
        Decimal(target) if target is not None else None,
        eligible,
    )


def transaction(
    transaction_type: str,
    amount: str | None,
    *,
    basis: str | None = None,
    fees: str | None = None,
    amount_status: str | None = None,
    basis_status: str | None = None,
    fees_status: str | None = None,
    trade_date: date = date(2026, 6, 1),
) -> TransactionFact:
    return TransactionFact(
        transaction_type,
        trade_date,
        Decimal(amount) if amount is not None else None,
        amount_status or ("supplied" if amount is not None else "unknown"),
        Decimal(basis) if basis is not None else None,
        basis_status or ("supplied" if basis is not None else "not_applicable"),
        Decimal(fees) if fees is not None else None,
        fees_status or ("supplied" if fees is not None else "not_applicable"),
    )


def test_portfolio_totals_allocations_drift_and_asset_classes() -> None:
    holdings = [
        holding(),
        holding(
            security_id="security-b",
            symbol="SYNB",
            asset_class="fixed_income",
            quantity="5",
            price="20",
            basis="90",
            target="40",
        ),
        holding(
            account_id="account-cash",
            account_name="Invented Cash",
            security_id="security-cash",
            symbol="CASH",
            asset_class="cash",
            quantity="50",
            price="1",
            basis=None,
            target=None,
            eligible=False,
        ),
        holding(
            account_id="account-bitcoin",
            account_name="Invented Bitcoin",
            security_id="security-bitcoin",
            symbol="SYNBTC",
            asset_class="bitcoin",
            quantity="0.01",
            price="5000",
            basis="30",
            target=None,
            eligible=False,
        ),
    ]

    report = portfolio_summary(
        holdings,
        [],
        [],
        [],
        now=NOW,
        price_stale_after_hours=48,
        position_stale_after_hours=48,
    )

    assert report.total_value == Decimal("300.00")
    assert report.invested_assets == Decimal("250.00")
    assert report.operational_cash == Decimal("50.00")
    assert report.bitcoin_value == Decimal("50.00")
    assert report.bitcoin_percentage == Decimal("16.666667")
    by_symbol = {item.fact.symbol: item for item in report.holdings}
    assert by_symbol["SYNA"].account_percentage == Decimal("50.000000")
    assert by_symbol["SYNA"].drift_percentage_points == Decimal("-10.000000")
    assert by_symbol["SYNA"].drift_dollars == Decimal("-20.00")
    assert sum((item.current_value for item in report.asset_classes), Decimal("0")) == Decimal("300.00")


def test_missing_and_partial_basis_never_becomes_contributions() -> None:
    report = portfolio_summary(
        [holding(), holding(security_id="security-b", symbol="SYNB", basis=None)],
        [transaction("deposit", "100")],
        [],
        [],
        now=NOW,
        price_stale_after_hours=48,
        position_stale_after_hours=48,
    )

    assert report.known_basis == Decimal("80.00")
    assert report.basis_status == "partial"
    assert report.unrealized_gain_loss == Decimal("20.00")
    assert report.flows.external_contributions == Decimal("100.00")


def test_cash_flow_classification_realized_gain_income_fees_and_transfers() -> None:
    flows = flow_summary(
        [
            transaction("deposit", "100"),
            transaction("withdrawal", "-20"),
            transaction("transfer_in", "40"),
            transaction("transfer_out", "-15"),
            transaction("dividend", "5"),
            transaction("reinvestment", "3"),
            transaction("interest", "2"),
            transaction("fee", "-1"),
            transaction("sell", "60", basis="50", fees="1"),
        ]
    )

    assert flows.external_contributions == Decimal("100.00")
    assert flows.external_withdrawals == Decimal("20.00")
    assert flows.net_external_contributions == Decimal("80.00")
    assert flows.internal_transfers_in == Decimal("40.00")
    assert flows.internal_transfers_out == Decimal("15.00")
    assert flows.income == Decimal("10.00")
    assert flows.distributions == Decimal("20.00")
    assert flows.known_fees == Decimal("2.00")
    assert flows.realized_gain_loss == Decimal("10.00")
    assert flows.realized_status == "complete"


def test_unknown_fees_and_sell_basis_remain_partial() -> None:
    flows = flow_summary(
        [
            transaction(
                "buy",
                None,
                basis=None,
                fees=None,
                basis_status="unknown",
                fees_status="unknown",
            ),
            transaction("sell", "50", basis=None, basis_status="unknown"),
        ]
    )

    assert flows.fee_status == "partial"
    assert flows.realized_status == "unavailable"
    assert flows.realized_gain_loss == Decimal("0.00")


def test_reinvested_dividend_is_income_but_never_external_contribution() -> None:
    flows = flow_summary([transaction("reinvestment", "7.25", basis="7.25")])

    assert flows.income == Decimal("7.25")
    assert flows.external_contributions == Decimal("0.00")
    assert flows.net_external_contributions == Decimal("0.00")


def test_performance_requires_complete_dated_history() -> None:
    insufficient = performance_summary([ValuationPoint(date(2026, 1, 1), Decimal("100"), True)], [])
    partial = performance_summary(
        [
            ValuationPoint(date(2026, 1, 1), Decimal("100"), True),
            ValuationPoint(date(2026, 12, 31), Decimal("110"), False),
        ],
        [],
    )

    assert insufficient.money_weighted.status == "insufficient_history"
    assert insufficient.time_weighted.status == "insufficient_history"
    assert partial.money_weighted.status == "insufficient_history"
    assert "partial" in partial.money_weighted.explanation.lower()


def test_money_weighted_and_time_weighted_returns_when_history_is_complete() -> None:
    performance = performance_summary(
        [
            ValuationPoint(date(2026, 1, 1), Decimal("100"), True),
            ValuationPoint(date(2026, 12, 31), Decimal("110"), True),
        ],
        [],
    )

    assert performance.money_weighted.status == "available"
    assert performance.money_weighted.value_percentage == Decimal("10.028806")
    assert performance.time_weighted.status == "available"
    assert performance.time_weighted.value_percentage == Decimal("10.000000")


def test_time_weighted_return_requires_valuation_on_external_flow_date() -> None:
    performance = performance_summary(
        [
            ValuationPoint(date(2026, 1, 1), Decimal("100"), True),
            ValuationPoint(date(2026, 12, 31), Decimal("175"), True),
        ],
        [transaction("deposit", "50", trade_date=date(2026, 6, 1))],
    )

    assert performance.money_weighted.status == "available"
    assert performance.time_weighted.status == "insufficient_history"
    assert "cash-flow date" in performance.time_weighted.explanation


def test_cash_envelope_reconciliation_and_overallocation() -> None:
    reconciled = reconcile_envelopes(
        Decimal("1000"),
        [
            EnvelopeFact("one", "Invented reserve", Decimal("800"), Decimal("600")),
            EnvelopeFact("two", "Invented travel", Decimal("500"), Decimal("250")),
        ],
    )
    overallocated = reconcile_envelopes(
        Decimal("500"), [EnvelopeFact("one", "Invented reserve", None, Decimal("700"))]
    )

    assert reconciled.assigned_total == Decimal("850.00")
    assert reconciled.unallocated_cash == Decimal("150.00")
    assert reconciled.overallocated_amount == Decimal("0.00")
    assert reconciled.envelopes[0].progress_percentage == Decimal("75.000000")
    assert overallocated.overallocated_amount == Decimal("200.00")


def test_allocator_is_deterministic_and_supports_fractional_and_whole_shares() -> None:
    holdings = [
        AllocationHolding("a", "SYNA", Decimal("20"), Decimal("60"), Decimal("7"), "fresh", True),
        AllocationHolding("b", "SYNB", Decimal("80"), Decimal("40"), Decimal("11"), "fresh", True),
    ]
    fractional = allocate_inflow(
        holdings,
        account_value=Decimal("100"),
        additional_cash=Decimal("50"),
        tracked_idle_cash=Decimal("0"),
        include_tracked_idle_cash=False,
        fractional_shares=True,
    )
    repeated = allocate_inflow(
        holdings,
        account_value=Decimal("100"),
        additional_cash=Decimal("50"),
        tracked_idle_cash=Decimal("0"),
        include_tracked_idle_cash=False,
        fractional_shares=True,
    )
    whole = allocate_inflow(
        holdings,
        account_value=Decimal("100"),
        additional_cash=Decimal("50"),
        tracked_idle_cash=Decimal("0"),
        include_tracked_idle_cash=False,
        fractional_shares=False,
    )

    assert fractional == repeated
    assert fractional.recommendations[0].symbol == "SYNA"
    assert fractional.cash_remainder < Decimal("0.01")
    assert whole.recommendations[0].estimated_shares == Decimal("7")
    assert whole.cash_remainder == Decimal("1.00")


def test_allocator_can_include_tracked_idle_cash_and_blocks_stale_or_missing_prices() -> None:
    plan = allocate_inflow(
        [
            AllocationHolding("a", "SYNA", Decimal("0"), Decimal("50"), Decimal("10"), "fresh", True),
            AllocationHolding("b", "SYNB", Decimal("0"), Decimal("25"), Decimal("10"), "stale", True),
            AllocationHolding("c", "SYNC", Decimal("0"), Decimal("25"), None, "missing", True),
        ],
        account_value=Decimal("100"),
        additional_cash=Decimal("20"),
        tracked_idle_cash=Decimal("30"),
        include_tracked_idle_cash=True,
        fractional_shares=False,
    )

    assert plan.available_cash == Decimal("50.00")
    assert plan.recommendations[0].recommended_dollars == Decimal("50.00")
    assert len(plan.warnings) == 2
    assert all("no allocation" in warning for warning in plan.warnings)


def test_stale_missing_zero_and_extreme_decimal_states() -> None:
    report = portfolio_summary(
        [
            holding(price=None, snapshot_value=None, basis=None, position_age_hours=100),
            holding(
                security_id="tiny",
                symbol="TINY",
                quantity="0.000000000001",
                price="0.00000001",
                basis="0",
                target="40",
            ),
            holding(
                security_id="large",
                symbol="LARGE",
                quantity="100000000000000000000",
                price="100000000000000000000.00000000",
                basis="0",
                target=None,
            ),
        ],
        [],
        [],
        [],
        now=NOW,
        price_stale_after_hours=48,
        position_stale_after_hours=48,
    )

    by_symbol = {item.fact.symbol: item for item in report.holdings}
    assert by_symbol["SYNA"].current_value is None
    assert by_symbol["SYNA"].position_state == "stale"
    assert by_symbol["SYNA"].price_state == "missing"
    assert by_symbol["TINY"].current_value == Decimal("0.00")
    assert by_symbol["LARGE"].current_value == Decimal("10000000000000000000000000000000000000000.00")


def test_bitcoin_guardrails_and_unconfigured_state() -> None:
    assert bitcoin_guardrail(None, None, None, None) == "not_configured"
    assert bitcoin_guardrail(Decimal("4"), Decimal("5"), Decimal("10"), Decimal("15")) == "within_guardrail"
    assert bitcoin_guardrail(Decimal("12"), Decimal("5"), Decimal("10"), Decimal("15")) == "caution"
    assert bitcoin_guardrail(Decimal("16"), Decimal("5"), Decimal("10"), Decimal("15")) == "review"
