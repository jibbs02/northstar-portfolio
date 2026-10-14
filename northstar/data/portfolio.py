from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from northstar.data.database import Database, utc_now

ACCOUNT_TYPES = frozenset({"roth_ira", "hsa", "taxable_brokerage", "cash", "bitcoin"})
TAX_TREATMENTS = frozenset({"tax_free", "tax_advantaged", "taxable", "not_applicable"})


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    account_type: str
    tax_treatment: str
    currency: str
    status: str
    has_provider_identifier: bool


@dataclass(frozen=True)
class ImportBatch:
    id: str
    adapter_key: str
    account_id: str
    status: str
    file_size_bytes: int
    row_count: int
    accepted_count: int
    rejected_count: int
    reconciliation_status: str
    created_at: str
    imported_at: str | None
    reversed_at: str | None


class PortfolioRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.vault = database.vault

    def create_account(
        self,
        *,
        name: str,
        account_type: str,
        tax_treatment: str,
        provider_identifier: str | None = None,
        notes: str | None = None,
        currency: str = "USD",
    ) -> Account:
        name = name.strip()
        provider_identifier = provider_identifier.strip() if provider_identifier else None
        notes = notes.strip() if notes else None
        if not name or len(name) > 200:
            raise ValueError("Account name is required and must be 200 characters or fewer")
        if account_type not in ACCOUNT_TYPES:
            raise ValueError("Unsupported account type")
        if tax_treatment not in TAX_TREATMENTS:
            raise ValueError("Unsupported tax treatment")
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("Currency must be a three-letter code")
        if provider_identifier and len(provider_identifier) > 200:
            raise ValueError("Provider identifier is too long")
        if notes and len(notes) > 2_000:
            raise ValueError("Notes are too long")
        account_id = str(uuid.uuid4())
        now = utc_now()
        identifier_hash = (
            self.vault.blind_index("account-provider-identifier", provider_identifier)
            if provider_identifier
            else None
        )
        with self.database.transaction() as conn:
            if (
                identifier_hash
                and conn.execute(
                    "SELECT 1 FROM accounts WHERE provider_identifier_hash=?", (identifier_hash,)
                ).fetchone()
            ):
                raise ValueError("An account with that provider identifier already exists")
            conn.execute(
                """
                INSERT INTO accounts(
                    id, account_type, tax_treatment, currency, encrypted_name,
                    encrypted_provider_identifier, provider_identifier_hash, encrypted_notes,
                    key_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    account_type,
                    tax_treatment,
                    currency.upper(),
                    self._encrypt("accounts", account_id, "name", name),
                    self._encrypt("accounts", account_id, "provider_identifier", provider_identifier),
                    identifier_hash,
                    self._encrypt("accounts", account_id, "notes", notes),
                    self.vault.key_version,
                    now,
                    now,
                ),
            )
        return self.get_account(account_id)

    def get_account(self, account_id: str) -> Account:
        with closing(self.database.connect()) as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise LookupError("Account was not found")
        return self._account(row)

    def list_accounts(self) -> list[Account]:
        with closing(self.database.connect()) as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY created_at, id").fetchall()
        return [self._account(row) for row in rows]

    def account_identifier_matches(self, account_id: str, candidate: str) -> bool:
        expected = self.vault.blind_index("account-provider-identifier", candidate)
        with closing(self.database.connect()) as conn:
            row = conn.execute(
                "SELECT provider_identifier_hash FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
        return bool(row and row[0] and row[0] == expected)

    def list_import_batches(self) -> list[ImportBatch]:
        with closing(self.database.connect()) as conn:
            rows = conn.execute(
                """SELECT id, adapter_key, account_id, status, file_size_bytes, row_count,
                          accepted_count, rejected_count, reconciliation_status, created_at,
                          imported_at, reversed_at
                   FROM import_batches ORDER BY created_at DESC, id DESC"""
            ).fetchall()
        return [ImportBatch(**dict(row)) for row in rows]

    def batch(self, batch_id: str) -> ImportBatch:
        with closing(self.database.connect()) as conn:
            row = conn.execute(
                """SELECT id, adapter_key, account_id, status, file_size_bytes, row_count,
                          accepted_count, rejected_count, reconciliation_status, created_at,
                          imported_at, reversed_at
                   FROM import_batches WHERE id=?""",
                (batch_id,),
            ).fetchone()
        if row is None:
            raise LookupError("Import batch was not found")
        return ImportBatch(**dict(row))

    def find_batch(self, adapter_key: str, account_id: str, fingerprint: str) -> ImportBatch | None:
        with closing(self.database.connect()) as conn:
            row = conn.execute(
                """SELECT id, adapter_key, account_id, status, file_size_bytes, row_count,
                          accepted_count, rejected_count, reconciliation_status, created_at,
                          imported_at, reversed_at
                   FROM import_batches
                   WHERE adapter_key=? AND account_id=? AND fingerprint=?""",
                (adapter_key, account_id, fingerprint),
            ).fetchone()
        return ImportBatch(**dict(row)) if row else None

    def _account(self, row: Any) -> Account:
        return Account(
            id=row["id"],
            name=str(self._decrypt("accounts", row["id"], "name", row["encrypted_name"])),
            account_type=row["account_type"],
            tax_treatment=row["tax_treatment"],
            currency=row["currency"],
            status=row["status"],
            has_provider_identifier=row["provider_identifier_hash"] is not None,
        )

    def _encrypt(self, table: str, record_id: str, field: str, value: str | None) -> bytes | None:
        if value is None:
            return None
        return self.vault.encrypt_json({"value": value}, associated_data=_field_aad(table, record_id, field))

    def _decrypt(self, table: str, record_id: str, field: str, envelope: bytes) -> Any:
        return self.vault.decrypt_json(envelope, associated_data=_field_aad(table, record_id, field))["value"]


def _field_aad(table: str, record_id: str, field: str) -> bytes:
    return f"portfolio-field:{table}:{record_id}:{field}".encode()


def encrypted_json(database: Database, table: str, record_id: str, field: str, value: Any) -> bytes:
    return database.vault.encrypt_json({"value": value}, associated_data=_field_aad(table, record_id, field))


def decrypted_json(database: Database, table: str, record_id: str, field: str, envelope: bytes) -> Any:
    return database.vault.decrypt_json(envelope, associated_data=_field_aad(table, record_id, field))["value"]


def canonical_record(record: dict[str, Any]) -> str:
    return json.dumps(record, separators=(",", ":"), sort_keys=True)


def rows_as_dicts(rows: Iterable[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
