from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from northstar.domain.precision import AMOUNT_QUANTUM, PERCENT_QUANTUM

ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")

CASH_ROLES = frozenset({"emergency_reserve", "spending_cash", "investable_cash"})
POLICY_SLEEVES = frozenset(
    {
        "us_equity",
        "international_equity",
        "nominal_treasury",
        "tips",
        "investable_cash",
        "bitcoin",
        "unclassified",
    }
)


@dataclass(frozen=True)
class HouseholdHolding:
    account_id: str
    security_id: str
    value: Decimal | None
    sleeve: str | None
    cash_role: str | None = None


@dataclass(frozen=True)
class SleeveExposure:
    sleeve: str
    value: Decimal
    percentage: Decimal | None


@dataclass(frozen=True)
class HouseholdPolicySummary:
    investment_allocation_denominator: Decimal
    household_risk_denominator: Decimal
    emergency_reserve: Decimal
    spending_cash: Decimal
    investable_cash: Decimal
    bitcoin_value: Decimal
    bitcoin_investment_percentage: Decimal | None
    bitcoin_household_risk_percentage: Decimal | None
    investment_sleeves: tuple[SleeveExposure, ...]
    complete: bool
    blockers: tuple[str, ...]


def household_policy_summary(holdings: list[HouseholdHolding]) -> HouseholdPolicySummary:
    """Build the two fixed Phase 4C denominator views.

    Investment allocation excludes emergency and spending cash. Household risk
    exposure includes emergency cash and excludes spending cash. Unknown values
    and missing/invalid classifications remain explicit blockers.
    """
    with localcontext() as context:
        context.prec = 80
        blockers: set[str] = set()
        sleeve_values: dict[str, Decimal] = {}
        emergency = ZERO
        spending = ZERO
        investable_cash = ZERO

        for holding in holdings:
            value = holding.value
            if value is None:
                blockers.add("unknown_value")
                continue
            if value < ZERO:
                blockers.add("negative_value")
                continue

            if holding.cash_role is not None:
                if holding.cash_role not in CASH_ROLES:
                    blockers.add("invalid_cash_role")
                    continue
                if holding.cash_role == "emergency_reserve":
                    emergency += value
                    continue
                if holding.cash_role == "spending_cash":
                    spending += value
                    continue
                investable_cash += value
                sleeve_values["investable_cash"] = (
                    sleeve_values.get("investable_cash", ZERO) + value
                )
                if holding.sleeve not in (None, "investable_cash"):
                    blockers.add("cash_sleeve_conflict")
                continue

            sleeve = holding.sleeve
            if sleeve is None or sleeve == "unclassified":
                blockers.add("unclassified_holding")
                sleeve = "unclassified"
            elif sleeve not in POLICY_SLEEVES:
                blockers.add("invalid_sleeve")
                sleeve = "unclassified"
            elif sleeve == "investable_cash":
                blockers.add("missing_cash_role")
            sleeve_values[sleeve] = sleeve_values.get(sleeve, ZERO) + value

        investment_denominator = _amount(sum(sleeve_values.values(), ZERO))
        household_risk_denominator = _amount(investment_denominator + emergency)
        bitcoin = _amount(sleeve_values.get("bitcoin", ZERO))
        exposures = tuple(
            SleeveExposure(sleeve, _amount(value), _percentage(value, investment_denominator))
            for sleeve, value in sorted(sleeve_values.items())
        )
        return HouseholdPolicySummary(
            investment_allocation_denominator=investment_denominator,
            household_risk_denominator=household_risk_denominator,
            emergency_reserve=_amount(emergency),
            spending_cash=_amount(spending),
            investable_cash=_amount(investable_cash),
            bitcoin_value=bitcoin,
            bitcoin_investment_percentage=_percentage(bitcoin, investment_denominator),
            bitcoin_household_risk_percentage=_percentage(bitcoin, household_risk_denominator),
            investment_sleeves=exposures,
            complete=not blockers,
            blockers=tuple(sorted(blockers)),
        )


def _amount(value: Decimal) -> Decimal:
    return value.quantize(AMOUNT_QUANTUM)


def _percentage(value: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator <= ZERO:
        return None
    return (value * ONE_HUNDRED / denominator).quantize(PERCENT_QUANTUM)
