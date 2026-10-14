from __future__ import annotations

import hmac
import re
import secrets
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from northstar.data.database import Database, utc_now
from northstar.data.portfolio import decrypted_json, encrypted_json
from northstar.integrations import SchwabTokenGrant

PROVIDER_TYPES = frozenset({"schwab"})
SCHWAB_MAPPABLE_ACCOUNT_TYPES = frozenset({"roth_ira", "hsa", "taxable_brokerage"})
SYNC_TRIGGERS = frozenset({"manual", "startup", "scheduled"})
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TOKEN_CATEGORY = "provider-token-bundle"  # noqa: S105 - record category, not a credential
OAUTH_STATE_TTL = timedelta(minutes=10)


class OAuthStateError(ValueError):
    """Raised when an OAuth transaction cannot be safely consumed."""


class ProviderConnectionStateError(ValueError):
    """Raised when a provider connection changed state during an operation."""


@dataclass(frozen=True)
class ProviderConnection:
    id: str
    provider_type: str
    status: str
    connected_at: str | None
    last_successful_sync_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ConsumedOAuthAttempt:
    connection: ProviderConnection
    state_hash: str


@dataclass(frozen=True)
class ProviderAccountMapping:
    id: str
    connection_id: str
    account_id: str
    last_four: str | None
    status: str
    created_at: str
    updated_at: str


class ProviderConnectionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.vault = database.vault

    def create(self, provider_type: str = "schwab") -> ProviderConnection:
        provider_type = provider_type.strip().lower()
        if provider_type not in PROVIDER_TYPES:
            raise ValueError("Unsupported provider type")
        connection_id = str(uuid.uuid4())
        now = utc_now()
        with self.database.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM provider_connections WHERE provider_type=?", (provider_type,)
            ).fetchone():
                raise ValueError("A connection for that provider already exists")
            conn.execute(
                """INSERT INTO provider_connections(
                       id, provider_type, status, key_version, connected_at,
                       last_successful_sync_at, created_at, updated_at
                   ) VALUES (?, ?, 'pending', ?, NULL, NULL, ?, ?)""",
                (connection_id, provider_type, self.vault.key_version, now, now),
            )
        return self.get(connection_id)

    def get(self, connection_id: str) -> ProviderConnection:
        with closing(self.database.connect()) as conn:
            row = conn.execute(
                """SELECT id, provider_type, status, connected_at, last_successful_sync_at,
                          created_at, updated_at
                   FROM provider_connections WHERE id=?""",
                (connection_id,),
            ).fetchone()
        if row is None:
            raise LookupError("Provider connection was not found")
        return ProviderConnection(**dict(row))

    def for_provider(self, provider_type: str = "schwab") -> ProviderConnection | None:
        provider_type = provider_type.strip().lower()
        if provider_type not in PROVIDER_TYPES:
            raise ValueError("Unsupported provider type")
        with closing(self.database.connect()) as conn:
            row = conn.execute(
                """SELECT id, provider_type, status, connected_at, last_successful_sync_at,
                          created_at, updated_at
                   FROM provider_connections WHERE provider_type=?""",
                (provider_type,),
            ).fetchone()
        return ProviderConnection(**dict(row)) if row is not None else None

    def start_oauth(self, session_binding: str) -> tuple[ProviderConnection, str]:
        session_binding = _oauth_secret(session_binding, "session binding")
        state = secrets.token_urlsafe(48)
        state_hash = self.vault.blind_index("oauth-state", state)
        session_hash = self.vault.blind_index("oauth-session", session_binding)
        now = utc_now()
        expires_at = (datetime.now(UTC) + OAUTH_STATE_TTL).isoformat(timespec="seconds")
        with self.database.transaction() as conn:
            conn.execute(
                "DELETE FROM oauth_transactions WHERE used_at IS NOT NULL OR expires_at<=?",
                (now,),
            )
            row = conn.execute(
                "SELECT id, status FROM provider_connections WHERE provider_type='schwab'"
            ).fetchone()
            if row is not None and row["status"] == "connected":
                raise ProviderConnectionStateError("Schwab is already connected")
            if row is None:
                connection_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO provider_connections(
                           id, provider_type, status, key_version, connected_at,
                           last_successful_sync_at, created_at, updated_at
                       ) VALUES (?, 'schwab', 'pending', ?, NULL, NULL, ?, ?)""",
                    (connection_id, self.vault.key_version, now, now),
                )
            else:
                connection_id = str(row["id"])
                conn.execute(
                    """UPDATE provider_connections SET status='pending', updated_at=?
                       WHERE id=?""",
                    (now, connection_id),
                )
            conn.execute(
                "UPDATE oauth_transactions SET used_at=? WHERE used_at IS NULL",
                (now,),
            )
            conn.execute(
                """INSERT INTO oauth_transactions(
                       state_hash, session_hash, encrypted_verifier, key_version,
                       expires_at, used_at, created_at
                   ) VALUES (?, ?, NULL, ?, ?, NULL, ?)""",
                (state_hash, session_hash, self.vault.key_version, expires_at, now),
            )
        return self.get(connection_id), state

    def consume_oauth(self, state: str, session_binding: str) -> ConsumedOAuthAttempt:
        state = _oauth_secret(state, "state")
        session_binding = _oauth_secret(session_binding, "session binding")
        state_hash = self.vault.blind_index("oauth-state", state)
        session_hash = self.vault.blind_index("oauth-session", session_binding)
        now = datetime.now(UTC)
        used_at = now.isoformat(timespec="seconds")
        expired = False
        connection: ProviderConnection | None = None
        with self.database.transaction() as conn:
            transaction = conn.execute(
                """SELECT session_hash, expires_at, used_at FROM oauth_transactions
                   WHERE state_hash=?""",
                (state_hash,),
            ).fetchone()
            if (
                transaction is None
                or transaction["used_at"] is not None
                or not hmac.compare_digest(str(transaction["session_hash"]), session_hash)
            ):
                raise OAuthStateError("OAuth transaction is invalid")
            try:
                expires_at = datetime.fromisoformat(str(transaction["expires_at"]))
            except ValueError:
                raise OAuthStateError("OAuth transaction is invalid") from None
            if expires_at.tzinfo is None or expires_at <= now:
                conn.execute(
                    "UPDATE oauth_transactions SET used_at=? WHERE state_hash=? AND used_at IS NULL",
                    (used_at, state_hash),
                )
                expired = True
            else:
                consumed = conn.execute(
                    "UPDATE oauth_transactions SET used_at=? WHERE state_hash=? AND used_at IS NULL",
                    (used_at, state_hash),
                )
                if consumed.rowcount != 1:
                    raise OAuthStateError("OAuth transaction is invalid")
                row = conn.execute(
                    """SELECT id, provider_type, status, connected_at, last_successful_sync_at,
                              created_at, updated_at
                       FROM provider_connections
                       WHERE provider_type='schwab' AND status='pending'"""
                ).fetchone()
                if row is None:
                    raise OAuthStateError("OAuth transaction is invalid")
                connection = ProviderConnection(**dict(row))
        if expired or connection is None:
            raise OAuthStateError("OAuth transaction is invalid")
        return ConsumedOAuthAttempt(connection=connection, state_hash=state_hash)

    def save_token_grant(self, attempt: ConsumedOAuthAttempt, grant: SchwabTokenGrant) -> ProviderConnection:
        connection_id = attempt.connection.id
        payload = asdict(grant)
        payload["expires_at"] = grant.expires_at.isoformat()
        payload["refresh_expires_at"] = (
            grant.refresh_expires_at.isoformat() if grant.refresh_expires_at else None
        )
        payload["scopes"] = list(grant.scopes)
        lookup_hash = self.vault.blind_index(_TOKEN_CATEGORY, connection_id)
        aad = f"secure-record:{_TOKEN_CATEGORY}:{lookup_hash}".encode()
        encrypted = self.vault.encrypt_json(payload, associated_data=aad)
        now = utc_now()
        with self.database.transaction() as conn:
            row = conn.execute(
                """SELECT provider_connections.status, oauth_transactions.used_at
                   FROM provider_connections
                   LEFT JOIN oauth_transactions ON oauth_transactions.state_hash=?
                   WHERE provider_connections.id=?""",
                (attempt.state_hash, connection_id),
            ).fetchone()
            if row is None:
                raise LookupError("Provider connection was not found")
            if row["status"] != "pending":
                raise ProviderConnectionStateError("Provider connection is not active")
            if row["used_at"] is None:
                raise OAuthStateError("OAuth transaction is no longer current")
            conn.execute(
                """INSERT INTO secure_records(
                       category, lookup_hash, encrypted_payload, key_version, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(category, lookup_hash) DO UPDATE SET
                       encrypted_payload=excluded.encrypted_payload,
                       key_version=excluded.key_version,
                       updated_at=excluded.updated_at""",
                (_TOKEN_CATEGORY, lookup_hash, encrypted, self.vault.key_version, now, now),
            )
            conn.execute(
                """UPDATE provider_connections
                   SET status='connected', connected_at=COALESCE(connected_at, ?), updated_at=?
                   WHERE id=?""",
                (now, now, connection_id),
            )
        return self.get(connection_id)

    def token_grant(self, connection_id: str) -> SchwabTokenGrant | None:
        self.get(connection_id)
        payload = self.database.get_secure_record(_TOKEN_CATEGORY, connection_id)
        if payload is None:
            return None
        return SchwabTokenGrant(
            access_token=str(payload["access_token"]),
            refresh_token=str(payload["refresh_token"]),
            token_type=str(payload["token_type"]),
            expires_at=datetime.fromisoformat(str(payload["expires_at"])),
            refresh_expires_at=(
                datetime.fromisoformat(str(payload["refresh_expires_at"]))
                if payload.get("refresh_expires_at")
                else None
            ),
            scopes=tuple(str(scope) for scope in payload.get("scopes", [])),
        )

    def replace_token_grant(self, connection_id: str, grant: SchwabTokenGrant) -> ProviderConnection:
        """Atomically replace a refreshed encrypted grant for the same live connection."""
        connection = self.get(connection_id)
        if connection.status != "connected":
            raise ProviderConnectionStateError("Provider connection is not active")
        payload = asdict(grant)
        payload["expires_at"] = grant.expires_at.isoformat()
        payload["refresh_expires_at"] = (
            grant.refresh_expires_at.isoformat() if grant.refresh_expires_at else None
        )
        payload["scopes"] = list(grant.scopes)
        lookup_hash = self.vault.blind_index(_TOKEN_CATEGORY, connection_id)
        aad = f"secure-record:{_TOKEN_CATEGORY}:{lookup_hash}".encode()
        encrypted = self.vault.encrypt_json(payload, associated_data=aad)
        now = utc_now()
        with self.database.transaction() as conn:
            active = conn.execute(
                "SELECT 1 FROM provider_connections WHERE id=? AND status='connected'", (connection_id,)
            ).fetchone()
            if active is None:
                raise ProviderConnectionStateError("Provider connection is not active")
            conn.execute(
                """UPDATE secure_records SET encrypted_payload=?, key_version=?, updated_at=?
                   WHERE category=? AND lookup_hash=?""",
                (encrypted, self.vault.key_version, now, _TOKEN_CATEGORY, lookup_hash),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise ProviderConnectionStateError("Provider token is unavailable")
        return self.get(connection_id)

    def replace_token_grant_if_current(
        self,
        connection_id: str,
        *,
        expected_refresh_token: str,
        grant: SchwabTokenGrant,
    ) -> bool:
        """Replace a refreshed grant only when it was derived from the stored grant.

        Refresh tokens rotate. A compare-and-swap prevents a slower concurrent refresh from
        overwriting the newer encrypted token bundle with a now-invalid sibling result.
        """
        expected_refresh_token = _oauth_secret(expected_refresh_token, "refresh token")
        payload = asdict(grant)
        payload["expires_at"] = grant.expires_at.isoformat()
        payload["refresh_expires_at"] = (
            grant.refresh_expires_at.isoformat() if grant.refresh_expires_at else None
        )
        payload["scopes"] = list(grant.scopes)
        lookup_hash = self.vault.blind_index(_TOKEN_CATEGORY, connection_id)
        aad = f"secure-record:{_TOKEN_CATEGORY}:{lookup_hash}".encode()
        encrypted = self.vault.encrypt_json(payload, associated_data=aad)
        now = utc_now()
        with self.database.transaction() as conn:
            row = conn.execute(
                """SELECT provider_connections.status, secure_records.encrypted_payload
                   FROM provider_connections
                   LEFT JOIN secure_records ON secure_records.category=?
                     AND secure_records.lookup_hash=?
                   WHERE provider_connections.id=?""",
                (_TOKEN_CATEGORY, lookup_hash, connection_id),
            ).fetchone()
            if row is None or row["status"] != "connected" or row["encrypted_payload"] is None:
                raise ProviderConnectionStateError("Provider token is unavailable")
            current = self.vault.decrypt_json(row["encrypted_payload"], associated_data=aad)
            current_refresh_token = current.get("refresh_token") if isinstance(current, dict) else None
            if not isinstance(current_refresh_token, str):
                raise ProviderConnectionStateError("Provider token is unavailable")
            if not hmac.compare_digest(current_refresh_token, expected_refresh_token):
                return False
            updated = conn.execute(
                """UPDATE secure_records SET encrypted_payload=?, key_version=?, updated_at=?
                   WHERE category=? AND lookup_hash=?""",
                (encrypted, self.vault.key_version, now, _TOKEN_CATEGORY, lookup_hash),
            )
            if updated.rowcount != 1:
                raise ProviderConnectionStateError("Provider token is unavailable")
        return True

    def disconnect(self, connection_id: str) -> ProviderConnection:
        lookup_hash = self.vault.blind_index(_TOKEN_CATEGORY, connection_id)
        now = utc_now()
        with self.database.transaction() as conn:
            if (
                conn.execute("SELECT 1 FROM provider_connections WHERE id=?", (connection_id,)).fetchone()
                is None
            ):
                raise LookupError("Provider connection was not found")
            conn.execute(
                "DELETE FROM secure_records WHERE category=? AND lookup_hash=?",
                (_TOKEN_CATEGORY, lookup_hash),
            )
            conn.execute(
                "UPDATE oauth_transactions SET used_at=? WHERE used_at IS NULL",
                (now,),
            )
            conn.execute(
                "UPDATE provider_connections SET status='disconnected', updated_at=? WHERE id=?",
                (now, connection_id),
            )
        return self.get(connection_id)

    def map_account(
        self,
        connection_id: str,
        account_id: str,
        *,
        external_account_hash: str,
        last_four: str | None = None,
    ) -> ProviderAccountMapping:
        connection = self.get(connection_id)
        if connection.status != "connected":
            raise ValueError("Provider connection is not connected")
        external_account_hash = _opaque_identifier(external_account_hash)
        last_four = _last_four(last_four)
        mapping_id = str(uuid.uuid4())
        now = utc_now()
        index = self.vault.blind_index(f"provider-account:{connection.provider_type}", external_account_hash)
        with self.database.transaction() as conn:
            if conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone() is None:
                raise LookupError("Local account was not found")
            if conn.execute(
                """SELECT 1 FROM provider_account_mappings
                   WHERE (connection_id=? AND account_id=?) OR external_account_hash_index=?""",
                (connection_id, account_id, index),
            ).fetchone():
                raise ValueError("Provider account mapping already exists")
            conn.execute(
                """INSERT INTO provider_account_mappings(
                       id, connection_id, account_id, encrypted_external_account_hash,
                       external_account_hash_index, encrypted_last_four, status, key_version,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                (
                    mapping_id,
                    connection_id,
                    account_id,
                    encrypted_json(
                        self.database,
                        "provider_account_mappings",
                        mapping_id,
                        "external_account_hash",
                        external_account_hash,
                    ),
                    index,
                    (
                        encrypted_json(
                            self.database,
                            "provider_account_mappings",
                            mapping_id,
                            "last_four",
                            last_four,
                        )
                        if last_four
                        else None
                    ),
                    self.vault.key_version,
                    now,
                    now,
                ),
            )
        return self.mapping(mapping_id)

    def map_accounts(
        self,
        connection_id: str,
        mappings: tuple[tuple[str, str, str | None], ...],
    ) -> tuple[ProviderAccountMapping, ...]:
        """Commit an explicit preview atomically; empty selections deliberately do nothing."""
        connection = self.get(connection_id)
        if connection.status != "connected":
            raise ValueError("Provider connection is not connected")
        normalized = [
            (_opaque_identifier(key), account_id, _last_four(last_four))
            for key, account_id, last_four in mappings
        ]
        if len({key for key, _, _ in normalized}) != len(normalized) or len(
            {account for _, account, _ in normalized}
        ) != len(normalized):
            raise ValueError("Provider account mapping is duplicated")
        if not normalized:
            return ()
        mapping_ids: list[str] = []
        pending: list[tuple[str, str, str | None]] = []
        now = utc_now()
        with self.database.transaction() as conn:
            for external_hash, account_id, last_four in normalized:
                account = conn.execute(
                    "SELECT status, account_type FROM accounts WHERE id=?", (account_id,)
                ).fetchone()
                if (
                    account is None
                    or account["status"] != "active"
                    or account["account_type"] not in SCHWAB_MAPPABLE_ACCOUNT_TYPES
                ):
                    raise ValueError("Local account is unavailable")
                index = self.vault.blind_index(f"provider-account:{connection.provider_type}", external_hash)
                existing = conn.execute(
                    """SELECT id, connection_id, account_id, external_account_hash_index, status,
                              encrypted_last_four
                       FROM provider_account_mappings
                       WHERE external_account_hash_index=?
                          OR (connection_id=? AND account_id=?)""",
                    (index, connection_id, account_id),
                ).fetchone()
                if existing is None:
                    pending.append((external_hash, account_id, last_four))
                    continue
                if (
                    existing["connection_id"] == connection_id
                    and existing["account_id"] == account_id
                    and existing["external_account_hash_index"] == index
                    and existing["status"] == "active"
                ):
                    existing_last_four = (
                        str(
                            decrypted_json(
                                self.database,
                                "provider_account_mappings",
                                existing["id"],
                                "last_four",
                                existing["encrypted_last_four"],
                            )
                        )
                        if existing["encrypted_last_four"] is not None
                        else None
                    )
                    if existing_last_four != last_four:
                        raise ValueError("Provider account identity changed")
                    mapping_ids.append(str(existing["id"]))
                    continue
                if existing:
                    raise ValueError("Provider account mapping already exists")
            for external_hash, account_id, last_four in pending:
                mapping_id = str(uuid.uuid4())
                mapping_ids.append(mapping_id)
                index = self.vault.blind_index(f"provider-account:{connection.provider_type}", external_hash)
                conn.execute(
                    """INSERT INTO provider_account_mappings(
                           id, connection_id, account_id, encrypted_external_account_hash,
                           external_account_hash_index, encrypted_last_four, status, key_version,
                           created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                    (
                        mapping_id,
                        connection_id,
                        account_id,
                        encrypted_json(
                            self.database,
                            "provider_account_mappings",
                            mapping_id,
                            "external_account_hash",
                            external_hash,
                        ),
                        index,
                        encrypted_json(
                            self.database, "provider_account_mappings", mapping_id, "last_four", last_four
                        )
                        if last_four
                        else None,
                        self.vault.key_version,
                        now,
                        now,
                    ),
                )
        return tuple(self.mapping(mapping_id) for mapping_id in mapping_ids)

    def mapped_account_ids(
        self, connection_id: str, provider_accounts: tuple[tuple[str, str | None], ...]
    ) -> tuple[str | None, ...]:
        """Return active mapping selections aligned to discovered opaque identities."""
        connection = self.get(connection_id)
        selections: list[str | None] = []
        with closing(self.database.connect()) as conn:
            for provider_account_hash, last_four in provider_accounts:
                index = self.vault.blind_index(
                    f"provider-account:{connection.provider_type}",
                    _opaque_identifier(provider_account_hash),
                )
                row = conn.execute(
                    """SELECT mapping.id, mapping.account_id, mapping.encrypted_last_four
                       FROM provider_account_mappings AS mapping
                       JOIN accounts ON accounts.id=mapping.account_id
                       WHERE mapping.connection_id=? AND mapping.external_account_hash_index=?
                         AND mapping.status='active' AND accounts.status='active'""",
                    (connection_id, index),
                ).fetchone()
                if row is None:
                    selections.append(None)
                    continue
                stored_last_four = (
                    str(
                        decrypted_json(
                            self.database,
                            "provider_account_mappings",
                            row["id"],
                            "last_four",
                            row["encrypted_last_four"],
                        )
                    )
                    if row["encrypted_last_four"] is not None
                    else None
                )
                if stored_last_four != _last_four(last_four):
                    raise ValueError("Provider account identity changed")
                selections.append(str(row["account_id"]))
        return tuple(selections)

    def mapping(self, mapping_id: str) -> ProviderAccountMapping:
        with closing(self.database.connect()) as conn:
            row = conn.execute("SELECT * FROM provider_account_mappings WHERE id=?", (mapping_id,)).fetchone()
        if row is None:
            raise LookupError("Provider account mapping was not found")
        last_four = (
            str(
                decrypted_json(
                    self.database,
                    "provider_account_mappings",
                    row["id"],
                    "last_four",
                    row["encrypted_last_four"],
                )
            )
            if row["encrypted_last_four"] is not None
            else None
        )
        return ProviderAccountMapping(
            id=row["id"],
            connection_id=row["connection_id"],
            account_id=row["account_id"],
            last_four=last_four,
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def start_sync(self, connection_id: str, *, actor: str, trigger: str = "manual") -> int:
        connection = self.get(connection_id)
        if connection.status != "connected":
            raise ValueError("Provider connection is not connected")
        if trigger not in SYNC_TRIGGERS:
            raise ValueError("Unsupported sync trigger")
        actor_hash = self.vault.blind_index("sync-actor", actor)
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO sync_runs(
                       provider, requested_by, status, records_seen, records_written, error_code,
                       started_at, finished_at, connection_id, trigger_type
                   ) VALUES (?, ?, 'running', 0, 0, NULL, ?, NULL, ?, ?)""",
                (connection.provider_type, actor_hash, utc_now(), connection_id, trigger),
            )
            sync_run_id = int(cursor.lastrowid)
        return sync_run_id

    def finish_sync(
        self,
        sync_run_id: int,
        *,
        succeeded: bool,
        records_seen: int,
        records_written: int,
        error_code: str | None = None,
    ) -> None:
        if min(records_seen, records_written) < 0 or records_written > records_seen:
            raise ValueError("Sync record counts are invalid")
        if succeeded and error_code is not None:
            raise ValueError("Successful sync cannot have an error code")
        if not succeeded and (error_code is None or _ERROR_CODE.fullmatch(error_code) is None):
            raise ValueError("Failed sync requires a privacy-safe error code")
        now = utc_now()
        with self.database.transaction() as conn:
            row = conn.execute(
                "SELECT connection_id, status FROM sync_runs WHERE id=?", (sync_run_id,)
            ).fetchone()
            if row is None:
                raise LookupError("Sync run was not found")
            if row["status"] != "running":
                raise ValueError("Sync run is already finished")
            conn.execute(
                """UPDATE sync_runs SET status=?, records_seen=?, records_written=?,
                       error_code=?, finished_at=? WHERE id=?""",
                (
                    "succeeded" if succeeded else "failed",
                    records_seen,
                    records_written,
                    error_code,
                    now,
                    sync_run_id,
                ),
            )
            if succeeded:
                conn.execute(
                    """UPDATE provider_connections
                       SET last_successful_sync_at=?, updated_at=? WHERE id=?""",
                    (now, now, row["connection_id"]),
                )


def _opaque_identifier(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 512
        or normalized.isdigit()
        or any(character.isspace() for character in normalized)
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized)
    ):
        raise ValueError("Provider account hash is malformed")
    return normalized


def _last_four(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if len(value) != 4 or not value.isascii() or not value.isalnum():
        raise ValueError("Account suffix must contain four ASCII letters or digits")
    return value


def _oauth_secret(value: str, label: str) -> str:
    if (
        not value
        or len(value) > 512
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise OAuthStateError(f"OAuth {label} is invalid")
    return value
