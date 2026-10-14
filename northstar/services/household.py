from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

from northstar.data import (
    GuidanceRepository,
    HouseholdPolicyRepository,
    HouseholdPolicySettings,
)
from northstar.domain import (
    HouseholdHolding,
    HouseholdPlan,
    HouseholdPolicySummary,
    PlanBitcoinBand,
    PlanTarget,
    PurchaseCandidate,
    SinkingFundConstraint,
    build_household_plan,
    household_policy_summary,
)
from northstar.domain.guidance import HoldingSummary
from northstar.services.guidance import DashboardView, GuidanceService


@dataclass(frozen=True)
class HouseholdPlanView:
    summary: HouseholdPolicySummary
    plan: HouseholdPlan


class HouseholdPlanService:
    def __init__(
        self,
        policy_repository: HouseholdPolicyRepository,
        guidance_repository: GuidanceRepository,
        guidance_service: GuidanceService,
    ) -> None:
        self.policy_repository = policy_repository
        self.guidance_repository = guidance_repository
        self.guidance_service = guidance_service

    def summary(self, *, now: datetime | None = None) -> HouseholdPolicySummary:
        dashboard, policy = self._inputs(now=now)
        return self._summary(dashboard.summary.holdings, policy)

    def plan(
        self,
        available_surplus: Decimal,
        one_time_inflow: Decimal = Decimal("0"),
        *,
        now: datetime | None = None,
    ) -> HouseholdPlanView:
        dashboard, policy = self._inputs(now=now)
        holding_summaries = dashboard.summary.holdings
        summary = self._summary(holding_summaries, policy)
        mappings = {item.security_id: item for item in policy.security_mappings}
        candidates = tuple(
            PurchaseCandidate(
                item.fact.account_id,
                item.fact.security_id,
                item.fact.symbol,
                mappings[item.fact.security_id].sleeve,
                item.fact.price,
                item.price_state == "fresh",
                mappings[item.fact.security_id].eligible_for_taxable_purchase
                and item.fact.account_type == "taxable_brokerage",
            )
            for item in holding_summaries
            if item.fact.security_id in mappings
            and mappings[item.fact.security_id].sleeve != "unclassified"
        )
        data_fresh = dashboard.summary.value_complete and all(
            item.position_state == "fresh"
            and (
                self._cash_role(item.fact.account_id, item.fact.security_id, policy) is not None
                or item.price_state == "fresh"
            )
            for item in holding_summaries
        )
        sinking = SinkingFundConstraint(Decimal("0"), Decimal("0"))
        if policy.sinking_fund is not None:
            envelope = next(
                (
                    item
                    for item in self.guidance_repository.envelope_facts()
                    if item.id == policy.sinking_fund.envelope_id
                ),
                None,
            )
            if envelope is None or envelope.target_amount is None:
                summary = replace(
                    summary,
                    complete=False,
                    blockers=tuple(sorted(set(summary.blockers) | {"sinking_fund_unavailable"})),
                )
            else:
                sinking = SinkingFundConstraint(
                    max(envelope.target_amount - envelope.assigned_amount, Decimal("0")),
                    policy.sinking_fund.configured_contribution,
                )
        result = build_household_plan(
            available_surplus=available_surplus,
            one_time_inflow=one_time_inflow,
            summary=summary,
            targets=tuple(PlanTarget(item.sleeve, item.target_percentage) for item in policy.targets),
            bitcoin_bands=tuple(
                PlanBitcoinBand(
                    item.lower_bound_percentage,
                    item.upper_bound_percentage,
                    item.contribution_percentage,
                )
                for item in policy.bitcoin_bands
            ),
            sinking_fund=sinking,
            candidates=candidates,
            data_fresh=data_fresh,
        )
        return HouseholdPlanView(summary, result)

    def _inputs(
        self, *, now: datetime | None
    ) -> tuple[DashboardView, HouseholdPolicySettings]:
        active_now = now or datetime.now(UTC)
        return self.guidance_service.dashboard(now=active_now), self.policy_repository.settings()

    @staticmethod
    def _cash_role(
        account_id: str, security_id: str, policy: HouseholdPolicySettings
    ) -> str | None:
        return next(
            (
                item.role
                for item in policy.cash_roles
                if item.account_id == account_id and item.security_id == security_id
            ),
            None,
        )

    def _summary(
        self,
        holding_summaries: tuple[HoldingSummary, ...],
        policy: HouseholdPolicySettings,
    ) -> HouseholdPolicySummary:
        mappings = {item.security_id: item.sleeve for item in policy.security_mappings}
        return household_policy_summary(
            [
                HouseholdHolding(
                    item.fact.account_id,
                    item.fact.security_id,
                    item.current_value,
                    mappings.get(item.fact.security_id),
                    self._cash_role(item.fact.account_id, item.fact.security_id, policy),
                )
                for item in holding_summaries
            ]
        )
