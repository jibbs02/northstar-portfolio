from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, localcontext

from northstar.domain.household import HouseholdPolicySummary
from northstar.domain.precision import AMOUNT_QUANTUM, PERCENT_QUANTUM, QUANTITY_QUANTUM

ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")
ACTIONABLE_SLEEVES = frozenset(
    {
        "us_equity",
        "international_equity",
        "nominal_treasury",
        "tips",
        "investable_cash",
        "bitcoin",
    }
)


@dataclass(frozen=True)
class PlanTarget:
    sleeve: str
    target_percentage: Decimal


@dataclass(frozen=True)
class PlanBitcoinBand:
    lower_bound_percentage: Decimal
    upper_bound_percentage: Decimal
    contribution_percentage: Decimal


@dataclass(frozen=True)
class SinkingFundConstraint:
    remaining_gap: Decimal
    configured_contribution: Decimal


@dataclass(frozen=True)
class PurchaseCandidate:
    account_id: str
    security_id: str
    symbol: str
    sleeve: str
    price: Decimal | None
    price_fresh: bool
    eligible_for_taxable_purchase: bool


@dataclass(frozen=True)
class PlanAction:
    action_type: str
    account_id: str | None
    security_id: str | None
    symbol: str | None
    sleeve: str | None
    recommended_dollars: Decimal
    estimated_shares: Decimal | None
    before_percentage: Decimal | None
    after_percentage: Decimal | None
    percentage_denominator: str | None
    rule: str


@dataclass(frozen=True)
class HouseholdPlan:
    status: str
    available_surplus: Decimal
    one_time_inflow: Decimal
    total_inflow: Decimal
    actions: tuple[PlanAction, ...]
    unallocated_remainder: Decimal
    warnings: tuple[str, ...]


def build_household_plan(
    *,
    available_surplus: Decimal,
    one_time_inflow: Decimal,
    summary: HouseholdPolicySummary,
    targets: tuple[PlanTarget, ...],
    bitcoin_bands: tuple[PlanBitcoinBand, ...],
    sinking_fund: SinkingFundConstraint,
    candidates: tuple[PurchaseCandidate, ...],
    data_fresh: bool,
) -> HouseholdPlan:
    """Return deterministic, inflow-only household guidance without side effects."""
    with localcontext() as context:
        context.prec = 80
        surplus = _nonnegative_amount(available_surplus, "Available surplus")
        extra = _nonnegative_amount(one_time_inflow, "One-time inflow")
        total = _amount(surplus + extra)
        gap = _nonnegative_amount(sinking_fund.remaining_gap, "Sinking-fund gap")
        configured = _nonnegative_amount(
            sinking_fund.configured_contribution, "Sinking-fund contribution"
        )

        blockers = list(summary.blockers)
        if not summary.complete and not blockers:
            blockers.append("household_summary_incomplete")
        if not data_fresh:
            blockers.append("stale_household_data")
        target_map = _validated_targets(targets)
        if target_map is None:
            blockers.append("household_targets_incomplete")
        band = _select_band(summary.bitcoin_household_risk_percentage, bitcoin_bands)
        if band is None:
            blockers.append("bitcoin_band_unavailable")
        if blockers:
            return _blocked(surplus, extra, total, blockers)
        assert target_map is not None
        assert band is not None

        sinking_amount = min(total, gap, configured)
        remaining = _amount(total - sinking_amount)
        bitcoin_amount = _amount(remaining * band.contribution_percentage / ONE_HUNDRED)
        taxable_amount = _amount(remaining - bitcoin_amount)

        sleeve_values = {
            exposure.sleeve: exposure.value for exposure in summary.investment_sleeves
        }
        sleeve_values["bitcoin"] = sleeve_values.get("bitcoin", ZERO) + bitcoin_amount
        projected_denominator = _amount(
            summary.investment_allocation_denominator + bitcoin_amount + taxable_amount
        )
        allocations, remainder = _taxable_allocations(
            taxable_amount,
            projected_denominator,
            sleeve_values,
            target_map,
            candidates,
        )
        selected = [candidate for candidate, amount in allocations if amount > ZERO]
        if any(not _candidate_has_price(candidate) for candidate in selected):
            return _blocked(surplus, extra, total, ["purchase_price_unavailable"])

        actions: list[PlanAction] = []
        if sinking_amount > ZERO:
            actions.append(
                PlanAction(
                    "sinking_fund",
                    None,
                    None,
                    None,
                    None,
                    sinking_amount,
                    None,
                    None,
                    None,
                    None,
                    "configured_sinking_fund_first",
                )
            )
        if bitcoin_amount > ZERO:
            actions.append(
                PlanAction(
                    "bitcoin",
                    None,
                    None,
                    None,
                    "bitcoin",
                    bitcoin_amount,
                    None,
                    summary.bitcoin_household_risk_percentage,
                    _percentage(
                        summary.bitcoin_value + bitcoin_amount,
                        summary.household_risk_denominator + bitcoin_amount + taxable_amount,
                    ),
                    "household_risk_exposure",
                    "bitcoin_contribution_band",
                )
            )

        final_values = dict(sleeve_values)
        for candidate, amount in allocations:
            final_values[candidate.sleeve] = final_values.get(candidate.sleeve, ZERO) + amount
        for candidate, amount in allocations:
            if amount <= ZERO:
                continue
            before_value = sleeve_values.get(candidate.sleeve, ZERO)
            actions.append(
                PlanAction(
                    "taxable_purchase",
                    candidate.account_id,
                    candidate.security_id,
                    candidate.symbol,
                    candidate.sleeve,
                    amount,
                    _shares(amount, candidate.price),
                    _percentage(
                        before_value,
                        summary.investment_allocation_denominator + bitcoin_amount,
                    ),
                    _percentage(final_values[candidate.sleeve], projected_denominator),
                    "investment_allocation",
                    "household_underweight_sleeve",
                )
            )
        warnings = ("taxable_remainder_unallocated",) if remainder > ZERO else ()
        return HouseholdPlan(
            "ready",
            surplus,
            extra,
            total,
            tuple(actions),
            remainder,
            warnings,
        )


def _validated_targets(targets: tuple[PlanTarget, ...]) -> dict[str, Decimal] | None:
    if {item.sleeve for item in targets} != ACTIONABLE_SLEEVES or len(targets) != len(
        ACTIONABLE_SLEEVES
    ):
        return None
    if any(item.target_percentage < ZERO or item.target_percentage > ONE_HUNDRED for item in targets):
        return None
    if sum((item.target_percentage for item in targets), ZERO) != ONE_HUNDRED:
        return None
    return {item.sleeve: item.target_percentage for item in targets}


def _select_band(
    bitcoin_percentage: Decimal | None, bands: tuple[PlanBitcoinBand, ...]
) -> PlanBitcoinBand | None:
    if bitcoin_percentage is None or not bands:
        return None
    ordered = tuple(sorted(bands, key=lambda item: item.lower_bound_percentage))
    if (
        ordered[0].lower_bound_percentage != ZERO
        or ordered[-1].upper_bound_percentage != ONE_HUNDRED
        or any(
            item.lower_bound_percentage < ZERO
            or item.lower_bound_percentage >= item.upper_bound_percentage
            or item.upper_bound_percentage > ONE_HUNDRED
            or item.contribution_percentage < ZERO
            or item.contribution_percentage > ONE_HUNDRED
            for item in ordered
        )
        or any(
            left.upper_bound_percentage != right.lower_bound_percentage
            for left, right in zip(ordered, ordered[1:], strict=False)
        )
    ):
        return None
    for index, item in enumerate(ordered):
        if item.lower_bound_percentage <= bitcoin_percentage and (
            bitcoin_percentage < item.upper_bound_percentage
            or (index == len(ordered) - 1 and bitcoin_percentage == item.upper_bound_percentage)
        ):
            return item
    return None


def _taxable_allocations(
    available: Decimal,
    projected_denominator: Decimal,
    sleeve_values: dict[str, Decimal],
    targets: dict[str, Decimal],
    candidates: tuple[PurchaseCandidate, ...],
) -> tuple[list[tuple[PurchaseCandidate, Decimal]], Decimal]:
    eligible_by_sleeve: dict[str, list[PurchaseCandidate]] = {}
    for candidate in candidates:
        if candidate.eligible_for_taxable_purchase:
            eligible_by_sleeve.setdefault(candidate.sleeve, []).append(candidate)
    for choices in eligible_by_sleeve.values():
        choices.sort(key=lambda item: (item.symbol, item.account_id, item.security_id))

    deficits = [
        (
            _amount(projected_denominator * target / ONE_HUNDRED - sleeve_values.get(sleeve, ZERO)),
            sleeve,
        )
        for sleeve, target in targets.items()
    ]
    deficits.sort(key=lambda item: (-item[0], item[1]))
    remaining = available
    allocations: list[tuple[PurchaseCandidate, Decimal]] = []
    for deficit, sleeve in deficits:
        if remaining <= ZERO:
            break
        choices = eligible_by_sleeve.get(sleeve, [])
        if deficit <= ZERO or not choices:
            continue
        amount = min(remaining, deficit)
        amount = _amount(amount)
        allocations.append((choices[0], amount))
        remaining = _amount(remaining - amount)
    return allocations, remaining


def _candidate_has_price(candidate: PurchaseCandidate) -> bool:
    return bool(candidate.price_fresh and candidate.price is not None and candidate.price > ZERO)


def _shares(amount: Decimal, price: Decimal | None) -> Decimal | None:
    if price is None or price <= ZERO:
        return None
    return (amount / price).quantize(QUANTITY_QUANTUM, rounding=ROUND_DOWN)


def _blocked(
    surplus: Decimal, extra: Decimal, total: Decimal, blockers: list[str]
) -> HouseholdPlan:
    return HouseholdPlan(
        "blocked",
        surplus,
        extra,
        total,
        (),
        total,
        tuple(sorted(set(blockers))),
    )


def _nonnegative_amount(value: Decimal, label: str) -> Decimal:
    if not value.is_finite() or value < ZERO:
        raise ValueError(f"{label} must be a non-negative finite amount")
    return _amount(value)


def _amount(value: Decimal) -> Decimal:
    return value.quantize(AMOUNT_QUANTUM)


def _percentage(value: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator <= ZERO:
        return None
    return (value * ONE_HUNDRED / denominator).quantize(PERCENT_QUANTUM)
