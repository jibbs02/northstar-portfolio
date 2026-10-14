from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from northstar.domain import (
    HouseholdHolding,
    PlanBitcoinBand,
    PlanTarget,
    PurchaseCandidate,
    SinkingFundConstraint,
    build_household_plan,
    household_policy_summary,
)


def summary():
    return household_policy_summary(
        [
            HouseholdHolding("taxable", "us", Decimal("600"), "us_equity"),
            HouseholdHolding("taxable", "international", Decimal("100"), "international_equity"),
            HouseholdHolding("taxable", "treasury", Decimal("100"), "nominal_treasury"),
            HouseholdHolding("taxable", "tips", Decimal("50"), "tips"),
            HouseholdHolding("taxable", "cash", Decimal("50"), "investable_cash", "investable_cash"),
            HouseholdHolding("bitcoin", "btc", Decimal("100"), "bitcoin"),
            HouseholdHolding("cash", "emergency", Decimal("200"), None, "emergency_reserve"),
            HouseholdHolding("cash", "spending", Decimal("25"), None, "spending_cash"),
        ]
    )


TARGETS = (
    PlanTarget("us_equity", Decimal("50")),
    PlanTarget("international_equity", Decimal("15")),
    PlanTarget("nominal_treasury", Decimal("15")),
    PlanTarget("tips", Decimal("10")),
    PlanTarget("investable_cash", Decimal("5")),
    PlanTarget("bitcoin", Decimal("5")),
)
BANDS = (
    PlanBitcoinBand(Decimal("0"), Decimal("10"), Decimal("20")),
    PlanBitcoinBand(Decimal("10"), Decimal("100"), Decimal("0")),
)
CANDIDATES = (
    PurchaseCandidate("taxable", "us", "SYNUS", "us_equity", Decimal("10"), True, True),
    PurchaseCandidate(
        "taxable", "international", "SYNINT", "international_equity", Decimal("20"), True, True
    ),
    PurchaseCandidate(
        "taxable", "treasury", "SYNT", "nominal_treasury", Decimal("25"), True, True
    ),
    PurchaseCandidate("taxable", "tips", "SYNTIPS", "tips", Decimal("10"), True, True),
    PurchaseCandidate("bitcoin", "btc", "SYNBTC", "bitcoin", Decimal("50"), True, False),
)


def plan(**overrides):
    arguments = {
        "available_surplus": Decimal("500"),
        "one_time_inflow": Decimal("100"),
        "summary": summary(),
        "targets": TARGETS,
        "bitcoin_bands": BANDS,
        "sinking_fund": SinkingFundConstraint(Decimal("75"), Decimal("50")),
        "candidates": CANDIDATES,
        "data_fresh": True,
    }
    arguments.update(overrides)
    return build_household_plan(**arguments)


def test_plan_orders_sinking_bitcoin_and_taxable_actions_deterministically() -> None:
    result = plan()

    assert result.status == "ready"
    assert result.total_inflow == Decimal("600.00")
    assert [action.action_type for action in result.actions] == [
        "sinking_fund",
        "bitcoin",
        "taxable_purchase",
        "taxable_purchase",
        "taxable_purchase",
    ]
    assert [action.recommended_dollars for action in result.actions] == [
        Decimal("50.00"),
        Decimal("110.00"),
        Decimal("175.00"),
        Decimal("132.50"),
        Decimal("132.50"),
    ]
    assert [action.sleeve for action in result.actions[2:]] == [
        "us_equity",
        "international_equity",
        "nominal_treasury",
    ]
    assert result.unallocated_remainder == Decimal("0.00")
    assert not result.warnings
    assert result.actions[1].estimated_shares is None
    assert result.actions[1].account_id is None
    assert result.actions[1].security_id is None
    assert result.actions[1].symbol is None
    assert result.actions[1].percentage_denominator == "household_risk_exposure"
    assert result.actions[2].percentage_denominator == "investment_allocation"
    assert all(
        action.before_percentage is not None and action.after_percentage is not None
        for action in result.actions[1:]
    )


def test_bitcoin_band_boundary_uses_upper_exclusive_next_band() -> None:
    at_boundary = replace(
        summary(),
        bitcoin_value=Decimal("120.00"),
        household_risk_denominator=Decimal("1200.00"),
        bitcoin_household_risk_percentage=Decimal("10.000000"),
    )

    result = plan(summary=at_boundary)

    assert all(action.action_type != "bitcoin" for action in result.actions)


def test_bitcoin_action_is_generic_when_multiple_holdings_exist() -> None:
    candidates = CANDIDATES + (
        PurchaseCandidate(
            "cold-storage", "btc-cold", "SYNBTC", "bitcoin", Decimal("50"), True, False
        ),
    )

    result = plan(candidates=candidates)
    bitcoin_action = next(action for action in result.actions if action.action_type == "bitcoin")

    assert bitcoin_action.account_id is None
    assert bitcoin_action.security_id is None
    assert bitcoin_action.symbol is None
    assert bitcoin_action.estimated_shares is None


@pytest.mark.parametrize(
    ("overrides", "warning"),
    [
        ({"data_fresh": False}, "stale_household_data"),
        (
            {
                "summary": household_policy_summary(
                    [HouseholdHolding("a", "unknown", None, "us_equity")]
                )
            },
            "unknown_value",
        ),
        ({"targets": TARGETS[:-1]}, "household_targets_incomplete"),
    ],
)
def test_incomplete_inputs_return_no_partial_actions(overrides: dict, warning: str) -> None:
    result = plan(**overrides)

    assert result.status == "blocked"
    assert not result.actions
    assert result.unallocated_remainder == result.total_inflow
    assert warning in result.warnings


def test_stale_selected_purchase_price_blocks_entire_plan() -> None:
    candidates = tuple(
        replace(item, price_fresh=False) if item.sleeve == "us_equity" else item
        for item in CANDIDATES
    )

    result = plan(candidates=candidates)

    assert result.status == "blocked"
    assert not result.actions
    assert result.warnings == ("purchase_price_unavailable",)


def test_plan_is_inflow_only_and_rejects_negative_inputs() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        plan(available_surplus=Decimal("-0.01"))
