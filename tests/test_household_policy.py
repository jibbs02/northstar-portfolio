from decimal import Decimal

from northstar.domain import HouseholdHolding, household_policy_summary


def holding(
    security_id: str,
    value: str | None,
    sleeve: str | None,
    cash_role: str | None = None,
) -> HouseholdHolding:
    return HouseholdHolding(
        account_id="synthetic-account",
        security_id=security_id,
        value=Decimal(value) if value is not None else None,
        sleeve=sleeve,
        cash_role=cash_role,
    )


def test_named_views_use_explicit_cash_denominators() -> None:
    summary = household_policy_summary(
        [
            holding("us", "600", "us_equity"),
            holding("btc", "100", "bitcoin"),
            holding("investable", "100", "investable_cash", "investable_cash"),
            holding("emergency", "150", None, "emergency_reserve"),
            holding("spending", "50", None, "spending_cash"),
        ]
    )

    assert summary.investment_allocation_denominator == Decimal("800.00")
    assert summary.household_risk_denominator == Decimal("950.00")
    assert summary.bitcoin_investment_percentage == Decimal("12.500000")
    assert summary.bitcoin_household_risk_percentage == Decimal("10.526316")
    assert summary.emergency_reserve == Decimal("150.00")
    assert summary.spending_cash == Decimal("50.00")
    assert summary.complete
    assert not summary.blockers


def test_unknown_and_unclassified_inputs_fail_summary_completeness() -> None:
    summary = household_policy_summary(
        [
            holding("known", "500", "us_equity"),
            holding("unknown-value", None, "bitcoin"),
            holding("unclassified", "25", None),
        ]
    )

    assert not summary.complete
    assert summary.blockers == ("unclassified_holding", "unknown_value")
    assert summary.investment_allocation_denominator == Decimal("525.00")


def test_investable_cash_requires_its_explicit_cash_role() -> None:
    summary = household_policy_summary(
        [holding("cash", "100", "investable_cash")]
    )

    assert not summary.complete
    assert summary.blockers == ("missing_cash_role",)
