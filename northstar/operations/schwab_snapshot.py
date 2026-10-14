from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from northstar.data import Database, ProviderConnectionRepository
from northstar.integrations import (
    SchwabAccountSnapshot,
    SchwabOAuthClient,
    SchwabOAuthError,
    SchwabReadClient,
    SchwabTokenGrant,
)
from northstar.services import SchwabSnapshotCoordinator, SchwabSnapshotService, SnapshotRefreshError

from .jobs import ExclusiveJobLock, JobLockedError

AUTHORIZATION_TTL = timedelta(minutes=15)
AUTHORIZATION_FILE = "schwab-live-snapshot-authorization.json"
OPERATOR_ACTOR = "local-schwab-live-snapshot-operator"


class LiveSnapshotOperatorError(RuntimeError):
    """A privacy-safe operator-entrypoint failure."""


@dataclass(frozen=True)
class LiveSnapshotAuthorization:
    expected_mapping_count: int
    order_throttle_confirmed_at: str
    expires_at: str


@dataclass(frozen=True)
class LiveSnapshotResult:
    deployed_commit: str
    execution_timestamp: str
    expected_mapping_count: int
    observed_mapping_count: int
    refresh_count: int
    account_read_count: int
    atomic_publication: str
    reconciliation: str
    no_disallowed_endpoint_called: bool

    def safe_dict(self) -> dict[str, object]:
        return asdict(self)


class CompatibilityCheck(Protocol):
    def __call__(self) -> bool: ...


class RecoveryCheck(Protocol):
    def __call__(self, confirmed_at: datetime) -> bool: ...


class LiveSnapshotAuthorizationStore:
    """Store one short-lived authorization without putting confirmation in shell history."""

    def __init__(self, operations_dir: Path) -> None:
        self.operations_dir = operations_dir
        self.path = operations_dir / AUTHORIZATION_FILE

    def create(self, expected_mapping_count: int, *, now: datetime | None = None) -> None:
        if not 1 <= expected_mapping_count <= 32:
            raise LiveSnapshotOperatorError("operator_expected_mapping_count_invalid")
        observed = (now or datetime.now(UTC)).astimezone(UTC)
        authorization = LiveSnapshotAuthorization(
            expected_mapping_count=expected_mapping_count,
            order_throttle_confirmed_at=observed.isoformat(timespec="seconds"),
            expires_at=(observed + AUTHORIZATION_TTL).isoformat(timespec="seconds"),
        )
        self.operations_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _require_private_directory(self.operations_dir)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError:
            raise LiveSnapshotOperatorError("operator_authorization_already_exists") from None
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(asdict(authorization), handle, separators=(",", ":"), sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            self.path.chmod(0o600)
            _fsync_directory(self.operations_dir)
        except Exception:
            self.path.unlink(missing_ok=True)
            _fsync_directory(self.operations_dir)
            raise

    def consume(self, *, now: datetime | None = None) -> LiveSnapshotAuthorization:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        try:
            metadata = self.path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_mode & 0o077
                or metadata.st_uid != os.getuid()
            ):
                raise LiveSnapshotOperatorError("operator_authorization_invalid")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or set(payload) != {
                "expected_mapping_count",
                "order_throttle_confirmed_at",
                "expires_at",
            }:
                raise ValueError
            authorization = LiveSnapshotAuthorization(**payload)
            confirmed = datetime.fromisoformat(authorization.order_throttle_confirmed_at)
            expires = datetime.fromisoformat(authorization.expires_at)
            if (
                confirmed.tzinfo is None
                or expires.tzinfo is None
                or expires <= current
                or confirmed > current
                or expires - confirmed != AUTHORIZATION_TTL
                or not 1 <= authorization.expected_mapping_count <= 32
            ):
                raise ValueError
            return authorization
        except LiveSnapshotOperatorError:
            raise
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            raise LiveSnapshotOperatorError("operator_authorization_invalid") from None
        finally:
            # Consumption occurs before token acquisition and is deliberately irreversible, even
            # when a later precondition or provider call fails.
            if os.path.lexists(self.path):
                self.path.unlink()
                _fsync_directory(self.operations_dir)


class _OneRefreshOAuthClient:
    def __init__(self, delegate: SchwabOAuthClient) -> None:
        self.delegate = delegate
        self.refresh_count = 0

    def authorization_url(self, *, state: str) -> str:
        raise AssertionError("authorization is outside the live snapshot operation")

    async def exchange_authorization_code(self, *, code: str) -> SchwabTokenGrant:
        raise AssertionError("authorization is outside the live snapshot operation")

    async def refresh_access(self, *, refresh_token: str) -> SchwabTokenGrant:
        if self.refresh_count >= 1:
            raise SchwabOAuthError("provider_authorization_rejected")
        self.refresh_count += 1
        return await self.delegate.refresh_access(refresh_token=refresh_token)


class _OneReadPerMappingClient:
    def __init__(self, delegate: SchwabReadClient) -> None:
        self.delegate = delegate
        self.account_read_count = 0
        self._requested_hashes: set[str] = set()
        self._allowed_hashes: frozenset[str] = frozenset()
        self._maximum_requests = 0
        self.disallowed_endpoint_called = False

    def authorize(self, provider_account_hashes: frozenset[str], maximum_requests: int) -> None:
        if self._allowed_hashes or self._maximum_requests or maximum_requests < 1:
            raise LiveSnapshotOperatorError("operator_mapping_authorization_invalid")
        if len(provider_account_hashes) != maximum_requests:
            raise LiveSnapshotOperatorError("operator_mapping_authorization_invalid")
        self._allowed_hashes = provider_account_hashes
        self._maximum_requests = maximum_requests

    async def account_snapshot(
        self, *, access_token: str, provider_account_hash: str
    ) -> SchwabAccountSnapshot:
        if (
            provider_account_hash not in self._allowed_hashes
            or provider_account_hash in self._requested_hashes
            or self.account_read_count >= self._maximum_requests
        ):
            raise SchwabOAuthError("provider_response_invalid")
        self._requested_hashes.add(provider_account_hash)
        self.account_read_count += 1
        return await self.delegate.account_snapshot(
            access_token=access_token,
            provider_account_hash=provider_account_hash,
        )

    async def linked_accounts(self, *, access_token: str):
        self.disallowed_endpoint_called = True
        raise AssertionError("account discovery is outside the live snapshot operation")

    async def transactions(self, **_: object):
        self.disallowed_endpoint_called = True
        raise AssertionError("transactions are outside the live snapshot operation")

    async def quotes(self, **_: object):
        self.disallowed_endpoint_called = True
        raise AssertionError("quotes are outside the live snapshot operation")


class LiveSnapshotOperator:
    """Invoke the dormant coordinator once through a local, single-use authorization."""

    def __init__(
        self,
        *,
        database: Database,
        connections: ProviderConnectionRepository,
        oauth_client: SchwabOAuthClient,
        read_client: SchwabReadClient,
        operations_dir: Path,
        deployed_commit: str,
        compatibility_check: CompatibilityCheck,
        recovery_check: RecoveryCheck,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.connections = connections
        self.oauth_client = oauth_client
        self.read_client = read_client
        self.operations_dir = operations_dir
        self.deployed_commit = _commit(deployed_commit)
        self.compatibility_check = compatibility_check
        self.recovery_check = recovery_check
        self.clock = clock or (lambda: datetime.now(UTC))

    async def run(self) -> LiveSnapshotResult:
        executed_at = self._now().isoformat(timespec="seconds")
        expected = 0
        observed = 0
        oauth = _OneRefreshOAuthClient(self.oauth_client)
        reads = _OneReadPerMappingClient(self.read_client)
        published = False
        reconciled = False
        connection_id: str | None = None
        coordinator_started = False
        try:
            with ExclusiveJobLock(self.operations_dir, "schwab-live-snapshot"):
                authorization = LiveSnapshotAuthorizationStore(self.operations_dir).consume(
                    now=self._now()
                )
                expected = authorization.expected_mapping_count
                connection = self.connections.for_provider("schwab")
                if connection is None or connection.status != "connected":
                    raise LiveSnapshotOperatorError("operator_connection_unavailable")
                connection_id = connection.id
                if not self.compatibility_check():
                    raise LiveSnapshotOperatorError("operator_compatibility_failed")
                confirmed_at = datetime.fromisoformat(authorization.order_throttle_confirmed_at)
                if not self.recovery_check(confirmed_at):
                    raise LiveSnapshotOperatorError("operator_recovery_checkpoint_failed")
                snapshots = SchwabSnapshotService(self.database, self.connections)
                mapping_set = snapshots.capture_mapping_set(connection.id)
                observed = len(mapping_set.mappings)
                if observed != expected:
                    raise LiveSnapshotOperatorError("operator_mapping_count_mismatch")
                reads.authorize(mapping_set.provider_account_hashes, expected)

                expires_at = datetime.fromisoformat(authorization.expires_at)

                def require_current_authorization() -> None:
                    if self._now() >= expires_at:
                        raise LiveSnapshotOperatorError("operator_authorization_expired")

                coordinator = SchwabSnapshotCoordinator(
                    connections=self.connections,
                    oauth_client=oauth,
                    read_client=reads,
                    snapshots=snapshots,
                )
                coordinator_started = True
                publication = await coordinator.refresh(
                    connection_id=connection.id,
                    actor=OPERATOR_ACTOR,
                    mapping_set=mapping_set,
                    before_provider_work=require_current_authorization,
                )
                if reads.account_read_count != expected or oauth.refresh_count > 1:
                    raise LiveSnapshotOperatorError("operator_request_count_invalid")
                published = publication.mapped_account_count == expected
                reconciled = publication.reconciliation_status == "matched"
                if not published or not reconciled:
                    raise LiveSnapshotOperatorError("operator_publication_invalid")
        except asyncio.CancelledError:
            raise
        except (JobLockedError, LiveSnapshotOperatorError, SnapshotRefreshError):
            if connection_id is not None and not coordinator_started:
                self._record_precondition_failure(connection_id)
        return LiveSnapshotResult(
            deployed_commit=self.deployed_commit,
            execution_timestamp=executed_at,
            expected_mapping_count=expected,
            observed_mapping_count=observed,
            refresh_count=oauth.refresh_count,
            account_read_count=reads.account_read_count,
            atomic_publication="published" if published else "not_published",
            reconciliation="pass" if reconciled else "fail",
            no_disallowed_endpoint_called=not reads.disallowed_endpoint_called,
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise LiveSnapshotOperatorError("operator_clock_invalid")
        return value.astimezone(UTC)

    def _record_precondition_failure(self, connection_id: str) -> None:
        try:
            sync_run = self.connections.start_sync(connection_id, actor=OPERATOR_ACTOR)
            self.connections.finish_sync(
                sync_run,
                succeeded=False,
                records_seen=0,
                records_written=0,
                error_code="operator_precondition_failed",
            )
        except Exception:
            # The original fail-closed result remains authoritative if even audit recording fails.
            return


def _commit(value: str) -> str:
    normalized = value.strip().lower()
    if not 7 <= len(normalized) <= 40 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("Deployed commit is malformed")
    return normalized


def _require_private_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_mode & 0o077
        or metadata.st_uid != os.getuid()
    ):
        raise LiveSnapshotOperatorError("operator_runtime_permissions_invalid")


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
