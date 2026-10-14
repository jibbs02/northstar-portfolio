from __future__ import annotations

import json
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from northstar.data.database import Database, utc_now
from northstar.data.portfolio import decrypted_json, encrypted_json
from northstar.domain import (
    DecimalKind,
    EnvelopeFact,
    HoldingFact,
    TransactionFact,
    ValuationPoint,
    canonical_decimal,
)


@dataclass(frozen=True)
class AccountPreference:
    account_id: str
    fractional_shares: bool
    dividend_reinvestment: bool


@dataclass(frozen=True)
class PortfolioRules:
    bitcoin_target_percentage: Decimal | None
    bitcoin_caution_percentage: Decimal | None
    bitcoin_review_percentage: Decimal | None
    rebalance_threshold_percentage_points: Decimal | None
    price_stale_after_hours: int = 48
    position_stale_after_hours: int = 48


@dataclass(frozen=True)
class SecurityChoice:
    id: str
    symbol: str
    name: str
    asset_class: str


@dataclass(frozen=True)
class TargetSetting:
    security_id: str
    target_percentage: Decimal
    eligible_for_contributions: bool


@dataclass(frozen=True)
class EnvelopeSetting:
    name: str
    target_amount: Decimal | None
    assigned_amount: Decimal


class GuidanceRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.vault = database.vault

    def holding_facts(self) -> list[HoldingFact]:
        with closing(self.database.connect()) as conn:
            positions = conn.execute(
                """
                WITH ranked AS (
                    SELECT p.*, b.adapter_key, ROW_NUMBER() OVER (
                        PARTITION BY p.account_id, p.security_id
                        ORDER BY p.source_date DESC,
                                 COALESCE(b.imported_at, b.created_at, p.as_of_utc) DESC,
                                 COALESCE(b.rowid, 0) DESC,
                                 p.id DESC
                    ) AS rank
                    FROM position_snapshots p
                    LEFT JOIN import_batches b ON b.id = p.import_batch_id
                    WHERE p.reversed_at IS NULL
                )
                SELECT * FROM ranked WHERE rank=1
                """
            ).fetchall()
            prices = conn.execute(
                """
                WITH ranked AS (
                    SELECT p.*, ROW_NUMBER() OVER (
                        PARTITION BY p.security_id
                        ORDER BY p.source_date DESC,
                                 COALESCE(b.imported_at, b.created_at, p.observed_at_utc) DESC,
                                 COALESCE(b.rowid, 0) DESC,
                                 p.id DESC
                    ) AS rank
                    FROM market_price_snapshots p
                    LEFT JOIN import_batches b ON b.id = p.import_batch_id
                    WHERE p.reversed_at IS NULL
                )
                SELECT * FROM ranked WHERE rank=1
                """
            ).fetchall()
            targets = conn.execute(
                """SELECT account_id, security_id, target_percentage,
                          eligible_for_contributions
                   FROM account_targets WHERE target_type='percentage'
                     AND security_id IS NOT NULL"""
            ).fetchall()
            accounts = {
                row["id"]: row
                for row in conn.execute("SELECT * FROM accounts WHERE status='active'").fetchall()
            }
            securities = {row["id"]: row for row in conn.execute("SELECT * FROM securities").fetchall()}

        position_by_key = {(row["account_id"], row["security_id"]): row for row in positions}
        price_by_security = {row["security_id"]: row for row in prices}
        target_by_key = {(row["account_id"], row["security_id"]): row for row in targets}
        keys = sorted(set(position_by_key) | set(target_by_key))
        facts: list[HoldingFact] = []
        for account_id, security_id in keys:
            account = accounts.get(account_id)
            security = securities.get(security_id)
            if account is None or security is None:
                continue
            position = position_by_key.get((account_id, security_id))
            price = price_by_security.get(security_id)
            target = target_by_key.get((account_id, security_id))
            symbol = (
                decrypted_json(
                    self.database,
                    "securities",
                    security_id,
                    "symbol",
                    security["encrypted_symbol"],
                )
                if security["encrypted_symbol"]
                else "Unknown"
            )
            security_name = (
                decrypted_json(
                    self.database,
                    "securities",
                    security_id,
                    "name",
                    security["encrypted_name"],
                )
                if security["encrypted_name"]
                else symbol
            )
            account_name = decrypted_json(
                self.database, "accounts", account_id, "name", account["encrypted_name"]
            )
            facts.append(
                HoldingFact(
                    account_id=account_id,
                    account_name=str(account_name),
                    account_type=account["account_type"],
                    tax_treatment=account["tax_treatment"],
                    security_id=security_id,
                    symbol=str(symbol),
                    security_name=str(security_name),
                    asset_class=security["asset_class"],
                    quantity=Decimal(position["quantity"]) if position else Decimal("0"),
                    snapshot_value=(
                        Decimal(position["market_value"])
                        if position and position["market_value"] is not None
                        else Decimal("0")
                        if position is None and target is not None
                        else None
                    ),
                    cost_basis=(
                        Decimal(position["cost_basis"])
                        if position and position["cost_basis"] is not None
                        else None
                    ),
                    position_as_of=(
                        _observation_time(position, "as_of_utc") if position else None
                    ),
                    price=Decimal(price["price"]) if price else None,
                    price_as_of=(
                        _observation_time(price, "observed_at_utc") if price else None
                    ),
                    position_source=position["adapter_key"] if position else None,
                    position_source_date=date.fromisoformat(position["source_date"]) if position else None,
                    price_source=price["source"] if price else None,
                    price_source_date=date.fromisoformat(price["source_date"]) if price else None,
                    target_percentage=(
                        Decimal(target["target_percentage"])
                        if target and target["target_percentage"] is not None
                        else None
                    ),
                    eligible_for_contributions=bool(target["eligible_for_contributions"])
                    if target
                    else False,
                )
            )
        return facts

    def transaction_facts(self) -> list[TransactionFact]:
        with closing(self.database.connect()) as conn:
            rows = conn.execute(
                """SELECT transaction_type, trade_date, net_amount, net_amount_status,
                          cost_basis, cost_basis_status, fees, fees_status
                   FROM transactions WHERE reversed_at IS NULL ORDER BY trade_date, id"""
            ).fetchall()
        return [
            TransactionFact(
                row["transaction_type"],
                date.fromisoformat(row["trade_date"]),
                Decimal(row["net_amount"]) if row["net_amount"] is not None else None,
                row["net_amount_status"],
                Decimal(row["cost_basis"]) if row["cost_basis"] is not None else None,
                row["cost_basis_status"],
                Decimal(row["fees"]) if row["fees"] is not None else None,
                row["fees_status"],
            )
            for row in rows
        ]

    def valuation_points(self) -> list[ValuationPoint]:
        with closing(self.database.connect()) as conn:
            rows = conn.execute(
                """SELECT as_of_date, total_value, completeness_status
                   FROM portfolio_valuation_snapshots ORDER BY as_of_date"""
            ).fetchall()
        return [
            ValuationPoint(
                date.fromisoformat(row["as_of_date"]),
                Decimal(row["total_value"]),
                row["completeness_status"] == "complete",
            )
            for row in rows
        ]

    def envelope_facts(self) -> list[EnvelopeFact]:
        with closing(self.database.connect()) as conn:
            rows = conn.execute("SELECT * FROM cash_envelopes ORDER BY created_at, id").fetchall()
        return [
            EnvelopeFact(
                row["id"],
                str(
                    decrypted_json(
                        self.database,
                        "cash_envelopes",
                        row["id"],
                        "name",
                        row["encrypted_name"],
                    )
                ),
                Decimal(row["target_amount"]) if row["target_amount"] is not None else None,
                Decimal(row["current_amount"]) if row["current_amount"] is not None else Decimal("0"),
                row["account_id"],
            )
            for row in rows
        ]

    def rules(self) -> PortfolioRules:
        with closing(self.database.connect()) as conn:
            row = conn.execute("SELECT * FROM portfolio_rules WHERE id=1").fetchone()
        if row is None:
            return PortfolioRules(None, None, None, None)
        return PortfolioRules(
            Decimal(row["bitcoin_target_percentage"]),
            Decimal(row["bitcoin_caution_percentage"]),
            Decimal(row["bitcoin_review_percentage"]),
            Decimal(row["rebalance_threshold_percentage_points"]),
            int(row["price_stale_after_hours"]),
            int(row["position_stale_after_hours"]),
        )

    def account_preference(self, account_id: str) -> AccountPreference:
        with closing(self.database.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM account_preferences WHERE account_id=?", (account_id,)
            ).fetchone()
        return AccountPreference(
            account_id,
            bool(row["fractional_shares"]) if row else False,
            bool(row["dividend_reinvestment"]) if row else False,
        )

    def security_choices(self, account_id: str) -> list[SecurityChoice]:
        facts = [fact for fact in self.holding_facts() if fact.account_id == account_id]
        return [
            SecurityChoice(fact.security_id, fact.symbol, fact.security_name, fact.asset_class)
            for fact in sorted(facts, key=lambda item: (item.symbol, item.security_id))
        ]

    def targets(self, account_id: str) -> list[TargetSetting]:
        with closing(self.database.connect()) as conn:
            rows = conn.execute(
                """SELECT security_id, target_percentage, eligible_for_contributions
                   FROM account_targets WHERE account_id=? AND target_type='percentage'
                   ORDER BY security_id""",
                (account_id,),
            ).fetchall()
        return [
            TargetSetting(
                row["security_id"],
                Decimal(row["target_percentage"]),
                bool(row["eligible_for_contributions"]),
            )
            for row in rows
        ]

    def save_account_settings(
        self,
        account_id: str,
        targets: list[TargetSetting],
        *,
        fractional_shares: bool,
        dividend_reinvestment: bool,
        actor: str,
    ) -> None:
        if not targets:
            raise ValueError("At least one account target is required")
        security_ids = [target.security_id for target in targets]
        if len(security_ids) != len(set(security_ids)):
            raise ValueError("A security target cannot be repeated")
        normalized = [
            TargetSetting(
                target.security_id,
                Decimal(canonical_decimal(target.target_percentage, DecimalKind.PERCENT)),
                target.eligible_for_contributions,
            )
            for target in targets
        ]
        if sum((target.target_percentage for target in normalized), Decimal("0")) != Decimal("100.000000"):
            raise ValueError("Account allocation targets must total exactly 100 percent")
        now = utc_now()
        with self.database.transaction() as conn:
            account = conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone()
            if account is None:
                raise LookupError("Account was not found")
            allowed = {
                row[0]
                for row in conn.execute(
                    """SELECT DISTINCT security_id FROM position_snapshots
                       WHERE account_id=? AND reversed_at IS NULL
                       UNION SELECT security_id FROM account_targets WHERE account_id=?""",
                    (account_id, account_id),
                ).fetchall()
            }
            if not set(security_ids) <= allowed:
                raise ValueError("A target references an unavailable holding")
            conn.execute("DELETE FROM account_targets WHERE account_id=?", (account_id,))
            for target in normalized:
                target_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO account_targets(
                           id, account_id, security_id, target_type, target_percentage,
                           target_amount, encrypted_notes, key_version, created_at, updated_at,
                           eligible_for_contributions
                       ) VALUES (?, ?, ?, 'percentage', ?, NULL, NULL, ?, ?, ?, ?)""",
                    (
                        target_id,
                        account_id,
                        target.security_id,
                        format(target.target_percentage, "f"),
                        self.vault.key_version,
                        now,
                        now,
                        int(target.eligible_for_contributions),
                    ),
                )
            conn.execute(
                """INSERT INTO account_preferences(
                       account_id, fractional_shares, dividend_reinvestment, updated_at
                   ) VALUES (?, ?, ?, ?)
                   ON CONFLICT(account_id) DO UPDATE SET
                       fractional_shares=excluded.fractional_shares,
                       dividend_reinvestment=excluded.dividend_reinvestment,
                       updated_at=excluded.updated_at""",
                (account_id, int(fractional_shares), int(dividend_reinvestment), now),
            )
            self._audit(
                conn,
                "settings.account_updated",
                actor,
                account_id,
                {
                    "target_count": len(normalized),
                    "fractional_shares": fractional_shares,
                    "dividend_reinvestment": dividend_reinvestment,
                },
            )

    def save_rules(self, rules: PortfolioRules, *, actor: str) -> None:
        values = (
            rules.bitcoin_target_percentage,
            rules.bitcoin_caution_percentage,
            rules.bitcoin_review_percentage,
            rules.rebalance_threshold_percentage_points,
        )
        if any(value is None for value in values):
            raise ValueError("All portfolio rule percentages are required")
        normalized = [
            Decimal(canonical_decimal(value, DecimalKind.PERCENT, allow_negative=False))
            for value in values
            if value is not None
        ]
        target, caution, review, threshold = normalized
        if review > Decimal("100") or not target <= caution <= review:
            raise ValueError("Bitcoin guardrails must be ordered and no greater than 100 percent")
        if threshold <= Decimal("0") or threshold > Decimal("100"):
            raise ValueError("Rebalancing threshold must be greater than zero and at most 100")
        freshness_valid = (
            1 <= rules.price_stale_after_hours <= 8760 and 1 <= rules.position_stale_after_hours <= 8760
        )
        if not freshness_valid:
            raise ValueError("Freshness windows are outside the supported range")
        now = utc_now()
        with self.database.transaction() as conn:
            conn.execute(
                """INSERT INTO portfolio_rules(
                       id, bitcoin_target_percentage, bitcoin_caution_percentage,
                       bitcoin_review_percentage, rebalance_threshold_percentage_points,
                       price_stale_after_hours, position_stale_after_hours, updated_at
                   ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       bitcoin_target_percentage=excluded.bitcoin_target_percentage,
                       bitcoin_caution_percentage=excluded.bitcoin_caution_percentage,
                       bitcoin_review_percentage=excluded.bitcoin_review_percentage,
                       rebalance_threshold_percentage_points=excluded.rebalance_threshold_percentage_points,
                       price_stale_after_hours=excluded.price_stale_after_hours,
                       position_stale_after_hours=excluded.position_stale_after_hours,
                       updated_at=excluded.updated_at""",
                (
                    format(target, "f"),
                    format(caution, "f"),
                    format(review, "f"),
                    format(threshold, "f"),
                    rules.price_stale_after_hours,
                    rules.position_stale_after_hours,
                    now,
                ),
            )
            self._audit(conn, "settings.portfolio_rules_updated", actor, None, {"configured": True})

    def save_envelopes(
        self,
        account_id: str,
        envelopes: list[EnvelopeSetting],
        *,
        actual_cash: Decimal,
        actor: str,
    ) -> None:
        if len({item.name.strip().casefold() for item in envelopes}) != len(envelopes):
            raise ValueError("Cash-envelope names must be unique")
        normalized: list[EnvelopeSetting] = []
        for item in envelopes:
            name = item.name.strip()
            if not name or len(name) > 200:
                raise ValueError("Cash-envelope name is invalid")
            target = (
                Decimal(canonical_decimal(item.target_amount, DecimalKind.AMOUNT, allow_negative=False))
                if item.target_amount is not None
                else None
            )
            assigned = Decimal(
                canonical_decimal(item.assigned_amount, DecimalKind.AMOUNT, allow_negative=False)
            )
            normalized.append(EnvelopeSetting(name, target, assigned))
        if sum((item.assigned_amount for item in normalized), Decimal("0")) > actual_cash:
            raise ValueError("Cash-envelope assignments exceed actual cash")
        now = utc_now()
        with self.database.transaction() as conn:
            account = conn.execute("SELECT account_type FROM accounts WHERE id=?", (account_id,)).fetchone()
            if account is None or account["account_type"] != "cash":
                raise ValueError("Cash envelopes require a cash account")
            conn.execute("DELETE FROM cash_envelopes WHERE account_id=?", (account_id,))
            for item in normalized:
                envelope_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO cash_envelopes(
                           id, account_id, encrypted_name, target_amount, current_amount,
                           encrypted_notes, key_version, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
                    (
                        envelope_id,
                        account_id,
                        encrypted_json(
                            self.database,
                            "cash_envelopes",
                            envelope_id,
                            "name",
                            item.name,
                        ),
                        format(item.target_amount, "f") if item.target_amount is not None else None,
                        format(item.assigned_amount, "f"),
                        self.vault.key_version,
                        now,
                        now,
                    ),
                )
            self._audit(
                conn,
                "settings.cash_envelopes_updated",
                actor,
                account_id,
                {"envelope_count": len(normalized)},
            )

    def _audit(
        self,
        conn: Any,
        event_type: str,
        actor: str,
        subject: str | None,
        metadata: dict[str, str | int | bool],
    ) -> None:
        conn.execute(
            """INSERT INTO audit_events(
                   event_type, actor_hash, subject_hash, metadata_json, created_at
               ) VALUES (?, ?, ?, ?, ?)""",
            (
                event_type,
                self.vault.blind_index("actor", actor),
                self.vault.blind_index("subject", subject) if subject else None,
                json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                utc_now(),
            ),
        )


def _observation_time(row: Any, timestamp_column: str) -> datetime:
    """Use a CSV source date conservatively; providers may retain precise UTC timestamps."""
    if row["import_batch_id"] is not None:
        return datetime.combine(date.fromisoformat(row["source_date"]), time.min, UTC)
    return datetime.fromisoformat(row[timestamp_column])
