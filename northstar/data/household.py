from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from northstar.data.database import Database, utc_now
from northstar.domain import CASH_ROLES, POLICY_SLEEVES, DecimalKind, canonical_decimal

TARGET_SLEEVES = POLICY_SLEEVES - {"unclassified"}
ZERO_PERCENT = Decimal("0.000000")
FULL_PERCENT = Decimal("100.000000")
MAX_BITCOIN_BANDS = 20


@dataclass(frozen=True)
class CashRoleSetting:
    account_id: str
    security_id: str
    role: str


@dataclass(frozen=True)
class SecurityPolicyMapping:
    security_id: str
    sleeve: str
    eligible_for_taxable_purchase: bool


@dataclass(frozen=True)
class HouseholdTarget:
    sleeve: str
    target_percentage: Decimal


@dataclass(frozen=True)
class BitcoinContributionBand:
    lower_bound_percentage: Decimal
    upper_bound_percentage: Decimal
    contribution_percentage: Decimal


@dataclass(frozen=True)
class SinkingFundSetting:
    envelope_id: str
    configured_contribution: Decimal


@dataclass(frozen=True)
class HouseholdPolicySettings:
    cash_roles: tuple[CashRoleSetting, ...]
    security_mappings: tuple[SecurityPolicyMapping, ...]
    targets: tuple[HouseholdTarget, ...]
    bitcoin_bands: tuple[BitcoinContributionBand, ...]
    sinking_fund: SinkingFundSetting | None


class HouseholdPolicyRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.vault = database.vault

    def settings(self) -> HouseholdPolicySettings:
        with closing(self.database.connect()) as conn:
            cash_rows = conn.execute(
                "SELECT account_id, security_id, role FROM cash_roles "
                "ORDER BY account_id, security_id"
            ).fetchall()
            mapping_rows = conn.execute(
                "SELECT security_id, sleeve, eligible_for_taxable_purchase "
                "FROM security_policy_mappings ORDER BY security_id"
            ).fetchall()
            target_rows = conn.execute(
                "SELECT sleeve, target_percentage FROM household_policy_targets ORDER BY sleeve"
            ).fetchall()
            band_rows = conn.execute(
                "SELECT lower_bound_percentage, upper_bound_percentage, contribution_percentage "
                "FROM bitcoin_contribution_bands ORDER BY sort_order"
            ).fetchall()
        sinking_payload = self.database.get_secure_record("household-plan-settings", "active")
        sinking_fund = (
            SinkingFundSetting(
                str(sinking_payload["sinking_fund_envelope_id"]),
                Decimal(str(sinking_payload["sinking_fund_contribution"])),
            )
            if sinking_payload is not None
            else None
        )
        return HouseholdPolicySettings(
            tuple(CashRoleSetting(row[0], row[1], row[2]) for row in cash_rows),
            tuple(SecurityPolicyMapping(row[0], row[1], bool(row[2])) for row in mapping_rows),
            tuple(HouseholdTarget(row[0], Decimal(row[1])) for row in target_rows),
            tuple(
                BitcoinContributionBand(Decimal(row[0]), Decimal(row[1]), Decimal(row[2]))
                for row in band_rows
            ),
            sinking_fund,
        )

    def save_settings(
        self,
        *,
        cash_roles: Iterable[CashRoleSetting],
        security_mappings: Iterable[SecurityPolicyMapping],
        targets: Iterable[HouseholdTarget],
        bitcoin_bands: Iterable[BitcoinContributionBand],
        sinking_fund: SinkingFundSetting | None = None,
        actor: str,
    ) -> None:
        normalized_roles = self._normalize_cash_roles(tuple(cash_roles))
        normalized_mappings = self._normalize_mappings(tuple(security_mappings))
        normalized_targets = self._normalize_targets(tuple(targets))
        normalized_bands = self._normalize_bands(tuple(bitcoin_bands))
        normalized_sinking = self._normalize_sinking_fund(sinking_fund)
        now = utc_now()

        with self.database.transaction() as conn:
            self._validate_references(conn, normalized_roles, normalized_mappings)
            self._validate_sinking_reference(conn, normalized_sinking)
            conn.execute("DELETE FROM cash_roles")
            conn.execute("DELETE FROM security_policy_mappings")
            conn.execute("DELETE FROM household_policy_targets")
            conn.execute("DELETE FROM bitcoin_contribution_bands")
            conn.executemany(
                "INSERT INTO cash_roles(account_id, security_id, role, updated_at) VALUES (?, ?, ?, ?)",
                [(item.account_id, item.security_id, item.role, now) for item in normalized_roles],
            )
            conn.executemany(
                """INSERT INTO security_policy_mappings(
                       security_id, sleeve, eligible_for_taxable_purchase, updated_at
                   ) VALUES (?, ?, ?, ?)""",
                [
                    (item.security_id, item.sleeve, int(item.eligible_for_taxable_purchase), now)
                    for item in normalized_mappings
                ],
            )
            conn.executemany(
                """INSERT INTO household_policy_targets(sleeve, target_percentage, updated_at)
                   VALUES (?, ?, ?)""",
                [
                    (item.sleeve, format(item.target_percentage, "f"), now)
                    for item in normalized_targets
                ],
            )
            conn.executemany(
                """INSERT INTO bitcoin_contribution_bands(
                       id, lower_bound_percentage, upper_bound_percentage,
                       contribution_percentage, sort_order, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        str(uuid.uuid4()),
                        format(item.lower_bound_percentage, "f"),
                        format(item.upper_bound_percentage, "f"),
                        format(item.contribution_percentage, "f"),
                        index,
                        now,
                    )
                    for index, item in enumerate(normalized_bands)
                ],
            )
            self._save_sinking_fund(conn, normalized_sinking, now)
            self._audit(
                conn,
                actor,
                {
                    "cash_role_count": len(normalized_roles),
                    "mapping_count": len(normalized_mappings),
                    "target_count": len(normalized_targets),
                    "bitcoin_band_count": len(normalized_bands),
                    "sinking_fund_configured": normalized_sinking is not None,
                },
            )

    def _normalize_cash_roles(
        self, items: tuple[CashRoleSetting, ...]
    ) -> tuple[CashRoleSetting, ...]:
        keys = [(item.account_id, item.security_id) for item in items]
        if len(keys) != len(set(keys)):
            raise ValueError("A cash holding cannot have more than one role")
        if any(item.role not in CASH_ROLES for item in items):
            raise ValueError("Unsupported cash role")
        return tuple(sorted(items, key=lambda item: (item.account_id, item.security_id)))

    def _normalize_mappings(
        self, items: tuple[SecurityPolicyMapping, ...]
    ) -> tuple[SecurityPolicyMapping, ...]:
        security_ids = [item.security_id for item in items]
        if len(security_ids) != len(set(security_ids)):
            raise ValueError("A security cannot map to more than one policy sleeve")
        if any(item.sleeve not in POLICY_SLEEVES for item in items):
            raise ValueError("Unsupported household policy sleeve")
        return tuple(sorted(items, key=lambda item: item.security_id))

    def _normalize_targets(
        self, items: tuple[HouseholdTarget, ...]
    ) -> tuple[HouseholdTarget, ...]:
        sleeves = [item.sleeve for item in items]
        if len(sleeves) != len(set(sleeves)):
            raise ValueError("A household policy sleeve target cannot be repeated")
        if set(sleeves) != TARGET_SLEEVES:
            raise ValueError("Household targets must cover every actionable policy sleeve")
        normalized = tuple(
            HouseholdTarget(
                item.sleeve,
                Decimal(
                    canonical_decimal(
                        item.target_percentage, DecimalKind.PERCENT, allow_negative=False
                    )
                ),
            )
            for item in items
        )
        if any(item.target_percentage > FULL_PERCENT for item in normalized):
            raise ValueError("Household target percentages cannot exceed 100 percent")
        if sum((item.target_percentage for item in normalized), ZERO_PERCENT) != FULL_PERCENT:
            raise ValueError("Household policy targets must total exactly 100 percent")
        return tuple(sorted(normalized, key=lambda item: item.sleeve))

    def _normalize_bands(
        self, items: tuple[BitcoinContributionBand, ...]
    ) -> tuple[BitcoinContributionBand, ...]:
        if not items:
            raise ValueError("At least one Bitcoin contribution band is required")
        if len(items) > MAX_BITCOIN_BANDS:
            raise ValueError(f"At most {MAX_BITCOIN_BANDS} Bitcoin contribution bands are supported")
        normalized = tuple(
            BitcoinContributionBand(
                Decimal(
                    canonical_decimal(
                        item.lower_bound_percentage, DecimalKind.PERCENT, allow_negative=False
                    )
                ),
                Decimal(
                    canonical_decimal(
                        item.upper_bound_percentage, DecimalKind.PERCENT, allow_negative=False
                    )
                ),
                Decimal(
                    canonical_decimal(
                        item.contribution_percentage, DecimalKind.PERCENT, allow_negative=False
                    )
                ),
            )
            for item in items
        )
        ordered = tuple(sorted(normalized, key=lambda item: item.lower_bound_percentage))
        if any(
            item.lower_bound_percentage >= item.upper_bound_percentage
            or item.upper_bound_percentage > FULL_PERCENT
            or item.contribution_percentage > FULL_PERCENT
            for item in ordered
        ):
            raise ValueError("Bitcoin contribution band percentages are outside the supported range")
        if (
            ordered[0].lower_bound_percentage != ZERO_PERCENT
            or ordered[-1].upper_bound_percentage != FULL_PERCENT
            or any(
                previous.upper_bound_percentage != following.lower_bound_percentage
                for previous, following in zip(ordered, ordered[1:], strict=False)
            )
        ):
            raise ValueError("Bitcoin contribution bands must cover 0 through 100 without gaps")
        return ordered

    def _normalize_sinking_fund(
        self, setting: SinkingFundSetting | None
    ) -> SinkingFundSetting | None:
        if setting is None:
            return None
        contribution = Decimal(
            canonical_decimal(
                setting.configured_contribution, DecimalKind.AMOUNT, allow_negative=False
            )
        )
        return SinkingFundSetting(setting.envelope_id, contribution)

    def _validate_references(
        self,
        conn: Any,
        cash_roles: tuple[CashRoleSetting, ...],
        mappings: tuple[SecurityPolicyMapping, ...],
    ) -> None:
        security_rows = {
            row["id"]: row
            for row in conn.execute("SELECT id, asset_class FROM securities").fetchall()
        }
        position_keys = {
            (row["account_id"], row["security_id"])
            for row in conn.execute(
                "SELECT DISTINCT account_id, security_id FROM position_snapshots "
                "WHERE reversed_at IS NULL"
            ).fetchall()
        }
        current_cash_keys = {
            key for key in position_keys if security_rows[key[1]]["asset_class"] == "cash"
        }
        if any(
            (item.account_id, item.security_id) not in position_keys
            or item.security_id not in security_rows
            or security_rows[item.security_id]["asset_class"] != "cash"
            for item in cash_roles
        ):
            raise ValueError("Cash roles require a current cash holding")
        if {(item.account_id, item.security_id) for item in cash_roles} != current_cash_keys:
            raise ValueError("Every current cash holding requires exactly one cash role")
        current_security_ids = {security_id for _, security_id in position_keys}
        if {item.security_id for item in mappings} != current_security_ids:
            raise ValueError("Every current security requires exactly one policy mapping")
        if any(
            security_rows[item.security_id]["asset_class"] == "cash"
            and item.sleeve != "investable_cash"
            for item in mappings
        ):
            raise ValueError("Cash securities can only map to the investable-cash sleeve")

    def _validate_sinking_reference(
        self, conn: Any, setting: SinkingFundSetting | None
    ) -> None:
        if setting is None:
            return
        envelope = conn.execute(
            "SELECT target_amount FROM cash_envelopes WHERE id=?", (setting.envelope_id,)
        ).fetchone()
        if envelope is None or envelope["target_amount"] is None:
            raise ValueError("The sinking-fund setting requires an envelope with a goal")

    def _save_sinking_fund(
        self, conn: Any, setting: SinkingFundSetting | None, now: str
    ) -> None:
        category = "household-plan-settings"
        lookup_hash = self.vault.blind_index(category, "active")
        if setting is None:
            conn.execute(
                "DELETE FROM secure_records WHERE category=? AND lookup_hash=?",
                (category, lookup_hash),
            )
            return
        aad = f"secure-record:{category}:{lookup_hash}".encode()
        encrypted = self.vault.encrypt_json(
            {
                "sinking_fund_envelope_id": setting.envelope_id,
                "sinking_fund_contribution": format(setting.configured_contribution, "f"),
            },
            associated_data=aad,
        )
        conn.execute(
            """INSERT INTO secure_records(
                   category, lookup_hash, encrypted_payload, key_version, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(category, lookup_hash) DO UPDATE SET
                   encrypted_payload=excluded.encrypted_payload,
                   key_version=excluded.key_version,
                   updated_at=excluded.updated_at""",
            (category, lookup_hash, encrypted, self.vault.key_version, now, now),
        )

    def _audit(self, conn: Any, actor: str, metadata: dict[str, int | bool]) -> None:
        conn.execute(
            """INSERT INTO audit_events(
                   event_type, actor_hash, subject_hash, metadata_json, created_at
               ) VALUES ('settings.household_policy_updated', ?, NULL, ?, ?)""",
            (
                self.vault.blind_index("actor", actor),
                json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                utc_now(),
            ),
        )
