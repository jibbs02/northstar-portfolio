from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

from northstar.data import (
    Account,
    AccountPreference,
    GuidanceRepository,
    ImportBatch,
    PortfolioRepository,
    PortfolioRules,
)
from northstar.domain import (
    AccountSummary,
    AllocationHolding,
    AllocationPlan,
    DecimalKind,
    PortfolioSummary,
    allocate_inflow,
    bitcoin_guardrail,
    canonical_decimal,
    portfolio_summary,
)


@dataclass(frozen=True)
class DashboardView:
    summary: PortfolioSummary
    rules: PortfolioRules
    bitcoin_guardrail_state: str
    accounts: tuple[Account, ...]
    latest_import: ImportBatch | None


@dataclass(frozen=True)
class AllocationView:
    account: Account
    preference: AccountPreference
    plan: AllocationPlan
    account_value: Decimal
    tracked_idle_cash: Decimal


class GuidanceService:
    def __init__(
        self,
        repository: GuidanceRepository,
        portfolio_repository: PortfolioRepository,
    ) -> None:
        self.repository = repository
        self.portfolio_repository = portfolio_repository

    def dashboard(self, *, now: datetime | None = None) -> DashboardView:
        active_now = now or datetime.now(UTC)
        rules = self.repository.rules()
        summary = portfolio_summary(
            self.repository.holding_facts(),
            self.repository.transaction_facts(),
            self.repository.valuation_points(),
            self.repository.envelope_facts(),
            now=active_now,
            price_stale_after_hours=rules.price_stale_after_hours,
            position_stale_after_hours=rules.position_stale_after_hours,
        )
        batches = self.portfolio_repository.list_import_batches()
        accounts = tuple(self.portfolio_repository.list_accounts())
        summarized_ids = {account.id for account in summary.accounts}
        empty_accounts = tuple(
            AccountSummary(
                account.id,
                account.name,
                account.account_type,
                account.tax_treatment,
                Decimal("0.00"),
                Decimal("0.000000") if summary.total_value else None,
                False,
            )
            for account in accounts
            if account.id not in summarized_ids
        )
        if empty_accounts:
            summary = replace(
                summary,
                accounts=tuple(sorted(summary.accounts + empty_accounts, key=lambda account: account.name)),
            )
        return DashboardView(
            summary,
            rules,
            bitcoin_guardrail(
                summary.bitcoin_percentage,
                rules.bitcoin_target_percentage,
                rules.bitcoin_caution_percentage,
                rules.bitcoin_review_percentage,
            ),
            accounts,
            batches[0] if batches else None,
        )

    def allocation_plan(
        self,
        account_id: str,
        additional_cash: str | Decimal,
        *,
        include_tracked_idle_cash: bool,
        now: datetime | None = None,
    ) -> AllocationView:
        account = self.portfolio_repository.get_account(account_id)
        preference = self.repository.account_preference(account_id)
        dashboard = self.dashboard(now=now)
        holding_summaries = [
            item for item in dashboard.summary.holdings if item.fact.account_id == account_id
        ]
        account_value = sum((item.current_value or Decimal("0") for item in holding_summaries), Decimal("0"))
        account_value = Decimal(canonical_decimal(account_value, DecimalKind.AMOUNT))
        idle_cash = Decimal(
            canonical_decimal(
                sum(
                    (
                        item.current_value or Decimal("0")
                        for item in holding_summaries
                        if item.fact.asset_class == "cash"
                    ),
                    Decimal("0"),
                ),
                DecimalKind.AMOUNT,
            )
        )
        amount = Decimal(canonical_decimal(additional_cash, DecimalKind.AMOUNT, allow_negative=False))
        plan = allocate_inflow(
            [
                AllocationHolding(
                    item.fact.security_id,
                    item.fact.symbol,
                    item.current_value or Decimal("0"),
                    item.fact.target_percentage or Decimal("0"),
                    item.fact.price,
                    item.price_state,
                    item.fact.eligible_for_contributions
                    and item.fact.asset_class != "cash"
                    and item.fact.target_percentage is not None,
                )
                for item in holding_summaries
            ],
            account_value=account_value,
            additional_cash=amount,
            tracked_idle_cash=idle_cash,
            include_tracked_idle_cash=include_tracked_idle_cash,
            fractional_shares=preference.fractional_shares,
        )
        return AllocationView(account, preference, plan, account_value, idle_cash)


def format_money(value: Decimal | None) -> str:
    if value is None:
        return "Unknown"
    sign = "−" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def format_percentage(value: Decimal | None) -> str:
    return "Unknown" if value is None else f"{value:.2f}%"


def format_quantity(value: Decimal | None) -> str:
    if value is None:
        return "Unknown"
    rendered = format(value, "f").rstrip("0").rstrip(".")
    return rendered or "0"
