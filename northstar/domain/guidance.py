from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, localcontext

from northstar.domain.precision import (
    AMOUNT_QUANTUM,
    PERCENT_QUANTUM,
    QUANTITY_QUANTUM,
)

ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")


@dataclass(frozen=True)
class HoldingFact:
    account_id: str
    account_name: str
    account_type: str
    tax_treatment: str
    security_id: str
    symbol: str
    security_name: str
    asset_class: str
    quantity: Decimal
    snapshot_value: Decimal | None
    cost_basis: Decimal | None
    position_as_of: datetime | None
    price: Decimal | None
    price_as_of: datetime | None
    target_percentage: Decimal | None = None
    eligible_for_contributions: bool = False
    position_source: str | None = None
    position_source_date: date | None = None
    price_source: str | None = None
    price_source_date: date | None = None


@dataclass(frozen=True)
class TransactionFact:
    transaction_type: str
    trade_date: date
    net_amount: Decimal | None
    net_amount_status: str
    cost_basis: Decimal | None
    cost_basis_status: str
    fees: Decimal | None
    fees_status: str


@dataclass(frozen=True)
class ValuationPoint:
    as_of_date: date
    value: Decimal
    complete: bool


@dataclass(frozen=True)
class HoldingSummary:
    fact: HoldingFact
    current_value: Decimal | None
    value_source: str
    account_percentage: Decimal | None
    portfolio_percentage: Decimal | None
    drift_percentage_points: Decimal | None
    drift_dollars: Decimal | None
    position_state: str
    price_state: str


@dataclass(frozen=True)
class AccountSummary:
    id: str
    name: str
    account_type: str
    tax_treatment: str
    current_value: Decimal
    allocation_percentage: Decimal | None
    complete: bool


@dataclass(frozen=True)
class AssetClassSummary:
    asset_class: str
    current_value: Decimal
    allocation_percentage: Decimal | None


@dataclass(frozen=True)
class FlowSummary:
    external_contributions: Decimal
    external_withdrawals: Decimal
    net_external_contributions: Decimal
    internal_transfers_in: Decimal
    internal_transfers_out: Decimal
    income: Decimal
    distributions: Decimal
    known_fees: Decimal
    realized_gain_loss: Decimal
    realized_status: str
    fee_status: str
    cash_flow_complete: bool


@dataclass(frozen=True)
class PerformanceMetric:
    status: str
    value_percentage: Decimal | None
    explanation: str


@dataclass(frozen=True)
class PerformanceSummary:
    money_weighted: PerformanceMetric
    time_weighted: PerformanceMetric


@dataclass(frozen=True)
class EnvelopeFact:
    id: str
    name: str
    target_amount: Decimal | None
    assigned_amount: Decimal
    account_id: str | None = None


@dataclass(frozen=True)
class EnvelopeSummary:
    fact: EnvelopeFact
    progress_percentage: Decimal | None


@dataclass(frozen=True)
class CashSummary:
    actual_cash: Decimal
    assigned_total: Decimal
    unallocated_cash: Decimal
    overallocated_amount: Decimal
    envelopes: tuple[EnvelopeSummary, ...]


@dataclass(frozen=True)
class PortfolioSummary:
    total_value: Decimal
    value_complete: bool
    invested_assets: Decimal
    operational_cash: Decimal
    bitcoin_value: Decimal
    bitcoin_percentage: Decimal | None
    known_basis: Decimal
    basis_status: str
    unrealized_gain_loss: Decimal
    accounts: tuple[AccountSummary, ...]
    holdings: tuple[HoldingSummary, ...]
    asset_classes: tuple[AssetClassSummary, ...]
    flows: FlowSummary
    performance: PerformanceSummary
    cash: CashSummary
    as_of: datetime | None


@dataclass(frozen=True)
class AllocationHolding:
    security_id: str
    symbol: str
    current_value: Decimal
    target_percentage: Decimal
    price: Decimal | None
    price_state: str
    eligible: bool


@dataclass(frozen=True)
class AllocationRecommendation:
    security_id: str
    symbol: str
    recommended_dollars: Decimal
    estimated_shares: Decimal
    resulting_percentage: Decimal
    reason: str


@dataclass(frozen=True)
class AllocationPlan:
    status: str
    available_cash: Decimal
    cash_remainder: Decimal
    recommendations: tuple[AllocationRecommendation, ...]
    warnings: tuple[str, ...]


def portfolio_summary(
    holdings: list[HoldingFact],
    transactions: list[TransactionFact],
    valuations: list[ValuationPoint],
    envelopes: list[EnvelopeFact],
    *,
    now: datetime,
    price_stale_after_hours: int,
    position_stale_after_hours: int,
) -> PortfolioSummary:
    with localcontext() as context:
        context.prec = 80
        valued: list[tuple[HoldingFact, Decimal | None, str, str, str]] = []
        for fact in holdings:
            position_state = _freshness(fact.position_as_of, now, position_stale_after_hours)
            price_state = _freshness(fact.price_as_of, now, price_stale_after_hours)
            if fact.snapshot_value is not None:
                value = _amount(fact.snapshot_value)
                source = "position_snapshot"
            elif fact.price is not None:
                value = _amount(fact.quantity * fact.price)
                source = "latest_price"
            else:
                value = None
                source = "unknown"
            valued.append((fact, value, source, position_state, price_state))

        known_values = [value for _, value, _, _, _ in valued if value is not None]
        total = _amount(sum(known_values, ZERO))
        value_complete = all(value is not None for _, value, _, _, _ in valued)
        account_values: dict[str, Decimal] = {}
        account_complete: dict[str, bool] = {}
        for fact, value, _, _, _ in valued:
            account_values[fact.account_id] = account_values.get(fact.account_id, ZERO) + (value or ZERO)
            account_complete[fact.account_id] = (
                account_complete.get(fact.account_id, True) and value is not None
            )

        holding_summaries: list[HoldingSummary] = []
        for fact, value, source, position_state, price_state in valued:
            account_value = _amount(account_values[fact.account_id])
            account_pct = _percentage(value, account_value) if value is not None else None
            portfolio_pct = _percentage(value, total) if value is not None else None
            target = fact.target_percentage
            drift_pp = (
                _percent(account_pct - target) if account_pct is not None and target is not None else None
            )
            drift_dollars = (
                _amount(value - (account_value * target / ONE_HUNDRED))
                if value is not None and target is not None
                else None
            )
            holding_summaries.append(
                HoldingSummary(
                    fact,
                    value,
                    source,
                    account_pct,
                    portfolio_pct,
                    drift_pp,
                    drift_dollars,
                    position_state,
                    price_state,
                )
            )

        first_by_account: dict[str, HoldingFact] = {}
        for fact in holdings:
            first_by_account.setdefault(fact.account_id, fact)
        accounts = tuple(
            AccountSummary(
                account_id,
                fact.account_name,
                fact.account_type,
                fact.tax_treatment,
                _amount(account_values[account_id]),
                _percentage(account_values[account_id], total),
                account_complete[account_id],
            )
            for account_id, fact in sorted(first_by_account.items(), key=lambda item: item[1].account_name)
        )

        by_class: dict[str, Decimal] = {}
        for fact, value, _, _, _ in valued:
            by_class[fact.asset_class] = by_class.get(fact.asset_class, ZERO) + (value or ZERO)
        asset_classes = tuple(
            AssetClassSummary(name, _amount(value), _percentage(value, total))
            for name, value in sorted(by_class.items())
        )
        operational_cash = _amount(by_class.get("cash", ZERO))
        bitcoin_value = _amount(by_class.get("bitcoin", ZERO))
        invested_assets = _amount(total - operational_cash)

        known_basis = ZERO
        known_unrealized = ZERO
        basis_known = 0
        basis_unknown = 0
        for fact, value, _, _, _ in valued:
            if fact.asset_class == "cash":
                continue
            if fact.cost_basis is not None and value is not None:
                known_basis += fact.cost_basis
                known_unrealized += value - fact.cost_basis
                basis_known += 1
            else:
                basis_unknown += 1
        basis_status = "unavailable" if basis_known == 0 else "partial" if basis_unknown else "complete"
        flows = flow_summary(transactions)
        performance = performance_summary(valuations, transactions)
        cash = reconcile_envelopes(operational_cash, envelopes)
        timestamps = [
            timestamp
            for fact in holdings
            for timestamp in (fact.position_as_of, fact.price_as_of)
            if timestamp is not None
        ]
        return PortfolioSummary(
            total,
            value_complete,
            invested_assets,
            operational_cash,
            bitcoin_value,
            _percentage(bitcoin_value, total),
            _amount(known_basis),
            basis_status,
            _amount(known_unrealized),
            accounts,
            tuple(holding_summaries),
            asset_classes,
            flows,
            performance,
            cash,
            max(timestamps) if timestamps else None,
        )


def flow_summary(transactions: list[TransactionFact]) -> FlowSummary:
    contributions = withdrawals = transfers_in = transfers_out = ZERO
    income = distributions = fees = realized = ZERO
    realized_known = realized_unknown = fee_unknown = 0
    cash_flow_complete = True
    for item in transactions:
        amount = abs(item.net_amount) if item.net_amount is not None else None
        if item.transaction_type == "deposit":
            if amount is None:
                cash_flow_complete = False
            else:
                contributions += amount
        elif item.transaction_type == "withdrawal":
            if amount is None:
                cash_flow_complete = False
            else:
                withdrawals += amount
                distributions += amount
        elif item.transaction_type == "transfer_in" and amount is not None:
            transfers_in += amount
        elif item.transaction_type == "transfer_out" and amount is not None:
            transfers_out += amount
        elif item.transaction_type in {"dividend", "interest", "reinvestment"}:
            if amount is not None:
                income += amount
        if item.transaction_type == "fee":
            if amount is None:
                fee_unknown += 1
            else:
                fees += amount
        elif item.fees_status == "supplied" and item.fees is not None:
            fees += abs(item.fees)
        elif item.fees_status == "unknown":
            fee_unknown += 1
        if item.transaction_type == "sell":
            if amount is not None and item.cost_basis_status == "supplied" and item.cost_basis is not None:
                realized += amount - item.cost_basis
                realized_known += 1
            else:
                realized_unknown += 1
    if realized_known == 0 and realized_unknown:
        realized_status = "unavailable"
    elif realized_unknown:
        realized_status = "partial"
    else:
        realized_status = "complete"
    return FlowSummary(
        _amount(contributions),
        _amount(withdrawals),
        _amount(contributions - withdrawals),
        _amount(transfers_in),
        _amount(transfers_out),
        _amount(income),
        _amount(distributions),
        _amount(fees),
        _amount(realized),
        realized_status,
        "partial" if fee_unknown else "complete",
        cash_flow_complete,
    )


def performance_summary(
    valuations: list[ValuationPoint], transactions: list[TransactionFact]
) -> PerformanceSummary:
    points = sorted(valuations, key=lambda point: point.as_of_date)
    if len(points) < 2:
        missing = "At least two complete dated portfolio valuations are required."
        return PerformanceSummary(
            PerformanceMetric("insufficient_history", None, missing),
            PerformanceMetric("insufficient_history", None, missing),
        )
    if any(not point.complete for point in points):
        missing = "One or more portfolio valuations are partial."
        return PerformanceSummary(
            PerformanceMetric("insufficient_history", None, missing),
            PerformanceMetric("insufficient_history", None, missing),
        )
    flows: list[tuple[date, Decimal]] = []
    for item in transactions:
        if item.transaction_type not in {"deposit", "withdrawal"}:
            continue
        if item.net_amount is None or item.net_amount_status != "supplied":
            missing = "A dated external contribution or withdrawal has an unknown amount."
            return PerformanceSummary(
                PerformanceMetric("insufficient_history", None, missing),
                PerformanceMetric("insufficient_history", None, missing),
            )
        portfolio_flow = abs(item.net_amount) if item.transaction_type == "deposit" else -abs(item.net_amount)
        flows.append((item.trade_date, portfolio_flow))

    first, last = points[0], points[-1]
    if any(flow_date < first.as_of_date or flow_date > last.as_of_date for flow_date, _ in flows):
        missing = "External cash-flow history extends beyond the available valuation history."
        return PerformanceSummary(
            PerformanceMetric("insufficient_history", None, missing),
            PerformanceMetric("insufficient_history", None, missing),
        )
    investor_cash_flows = [(first.as_of_date, -first.value)]
    investor_cash_flows.extend((flow_date, -amount) for flow_date, amount in flows)
    investor_cash_flows.append((last.as_of_date, last.value))
    money_weighted_value = _xirr(investor_cash_flows)
    money_weighted = (
        PerformanceMetric("available", money_weighted_value, "Annualized money-weighted return.")
        if money_weighted_value is not None
        else PerformanceMetric(
            "insufficient_history", None, "The dated cash flows do not produce a solvable return."
        )
    )

    valuation_dates = {point.as_of_date for point in points}
    missing_flow_dates = sorted({flow_date for flow_date, _ in flows} - valuation_dates)
    if missing_flow_dates:
        time_weighted = PerformanceMetric(
            "insufficient_history",
            None,
            "A complete portfolio valuation is required on every external cash-flow date.",
        )
    else:
        flow_by_date: dict[date, Decimal] = {}
        for flow_date, amount in flows:
            flow_by_date[flow_date] = flow_by_date.get(flow_date, ZERO) + amount
        growth = Decimal("1")
        eligible = True
        for previous, current in zip(points, points[1:], strict=False):
            if previous.value <= ZERO:
                eligible = False
                break
            period_flow = flow_by_date.get(current.as_of_date, ZERO)
            growth *= (current.value - period_flow) / previous.value
        time_weighted = (
            PerformanceMetric(
                "available",
                _percent((growth - Decimal("1")) * ONE_HUNDRED),
                "Cumulative time-weighted return over available complete valuations.",
            )
            if eligible
            else PerformanceMetric(
                "insufficient_history", None, "A starting portfolio valuation is zero or negative."
            )
        )
    return PerformanceSummary(money_weighted, time_weighted)


def reconcile_envelopes(actual_cash: Decimal, envelopes: list[EnvelopeFact]) -> CashSummary:
    assigned = _amount(sum((item.assigned_amount for item in envelopes), ZERO))
    unallocated = _amount(max(actual_cash - assigned, ZERO))
    overallocated = _amount(max(assigned - actual_cash, ZERO))
    summaries = tuple(
        EnvelopeSummary(
            item,
            _percentage(item.assigned_amount, item.target_amount)
            if item.target_amount is not None and item.target_amount > ZERO
            else None,
        )
        for item in sorted(envelopes, key=lambda item: item.name)
    )
    return CashSummary(_amount(actual_cash), assigned, unallocated, overallocated, summaries)


def allocate_inflow(
    holdings: list[AllocationHolding],
    *,
    account_value: Decimal,
    additional_cash: Decimal,
    tracked_idle_cash: Decimal,
    include_tracked_idle_cash: bool,
    fractional_shares: bool,
) -> AllocationPlan:
    if additional_cash < ZERO:
        raise ValueError("Additional cash cannot be negative")
    available = _amount(additional_cash + (tracked_idle_cash if include_tracked_idle_cash else ZERO))
    projected_total = _amount(account_value + additional_cash)
    warnings: list[str] = []
    candidates: list[tuple[Decimal, AllocationHolding]] = []
    for item in holdings:
        if not item.eligible:
            continue
        if item.price is None:
            warnings.append(f"{item.symbol}: price is missing; no allocation was calculated.")
            continue
        if item.price_state != "fresh":
            warnings.append(f"{item.symbol}: price is stale; no allocation was calculated.")
            continue
        target_value = projected_total * item.target_percentage / ONE_HUNDRED
        deficit = _amount(max(target_value - item.current_value, ZERO))
        if deficit > ZERO:
            candidates.append((deficit, item))
    candidates.sort(key=lambda pair: (-pair[0], pair[1].symbol, pair[1].security_id))

    remaining = available
    recommendations: list[AllocationRecommendation] = []
    for deficit, item in candidates:
        if remaining <= ZERO:
            break
        budget = min(deficit, remaining)
        if fractional_shares:
            shares = (budget / item.price).quantize(QUANTITY_QUANTUM, rounding=ROUND_DOWN)
        else:
            shares = (budget / item.price).to_integral_value(rounding=ROUND_DOWN)
        dollars = _amount(shares * item.price)
        while dollars > remaining and shares > ZERO:
            shares -= QUANTITY_QUANTUM if fractional_shares else Decimal("1")
            dollars = _amount(shares * item.price)
        if shares <= ZERO or dollars <= ZERO:
            continue
        remaining = _amount(remaining - dollars)
        resulting = _percentage(item.current_value + dollars, projected_total) or ZERO
        recommendations.append(
            AllocationRecommendation(
                item.security_id,
                item.symbol,
                dollars,
                shares.quantize(QUANTITY_QUANTUM) if fractional_shares else shares,
                resulting,
                "Directs available cash toward an eligible holding below its configured target.",
            )
        )
    status = "ready" if recommendations else "no_recommendation"
    if available == ZERO:
        warnings.append("No allocatable cash was supplied.")
    return AllocationPlan(status, available, remaining, tuple(recommendations), tuple(warnings))


def bitcoin_guardrail(
    actual_percentage: Decimal | None,
    target: Decimal | None,
    caution: Decimal | None,
    review: Decimal | None,
) -> str:
    if None in {actual_percentage, target, caution, review}:
        return "not_configured"
    assert actual_percentage is not None and caution is not None and review is not None
    if actual_percentage > review:
        return "review"
    if actual_percentage > caution:
        return "caution"
    return "within_guardrail"


def _freshness(timestamp: datetime | None, now: datetime, stale_after_hours: int) -> str:
    if timestamp is None:
        return "missing"
    elapsed = now - timestamp
    return "stale" if elapsed.total_seconds() > stale_after_hours * 3600 else "fresh"


def _amount(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 80
        return value.quantize(AMOUNT_QUANTUM, rounding=ROUND_HALF_EVEN)


def _percent(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 80
        return value.quantize(PERCENT_QUANTUM, rounding=ROUND_HALF_EVEN)


def _percentage(value: Decimal | None, total: Decimal | None) -> Decimal | None:
    if value is None or total is None or total == ZERO:
        return None
    with localcontext() as context:
        context.prec = 80
        return _percent(value / total * ONE_HUNDRED)


def _xirr(cash_flows: list[tuple[date, Decimal]]) -> Decimal | None:
    if (
        not cash_flows
        or not any(amount < ZERO for _, amount in cash_flows)
        or not any(amount > ZERO for _, amount in cash_flows)
    ):
        return None
    origin = min(flow_date for flow_date, _ in cash_flows)

    def npv(rate: Decimal) -> Decimal:
        base = Decimal("1") + rate
        return sum(
            (
                amount / (base ** (Decimal((flow_date - origin).days) / Decimal("365")))
                for flow_date, amount in cash_flows
            ),
            ZERO,
        )

    with localcontext() as context:
        context.prec = 60
        low = Decimal("-0.999999")
        high = Decimal("10")
        low_value = npv(low)
        high_value = npv(high)
        while low_value * high_value > ZERO and high < Decimal("1000000"):
            high *= Decimal("10")
            high_value = npv(high)
        if low_value * high_value > ZERO:
            return None
        for _ in range(240):
            middle = (low + high) / Decimal("2")
            middle_value = npv(middle)
            if abs(middle_value) < Decimal("0.000000000001"):
                return _percent(middle * ONE_HUNDRED)
            if low_value * middle_value <= ZERO:
                high = middle
            else:
                low = middle
                low_value = middle_value
        return _percent(((low + high) / Decimal("2")) * ONE_HUNDRED)
