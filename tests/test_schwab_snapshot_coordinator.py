from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from northstar.config import Settings
from northstar.data import Database, PortfolioRepository, ProviderConnectionRepository
from northstar.data import database as database_module
from northstar.integrations import (
    SchwabAccountSnapshot,
    SchwabOAuthError,
    SchwabTokenGrant,
    SchwabValidationCategory,
    SchwabValidationError,
)
from northstar.main import create_app
from northstar.operations import (
    ExclusiveJobLock,
    LiveSnapshotAuthorizationStore,
    LiveSnapshotOperator,
    LiveSnapshotOperatorError,
)
from northstar.services import (
    SCHWAB_HTTP_SNAPSHOT_PROVENANCE,
    SchwabSnapshotCoordinator,
    SchwabSnapshotService,
    SnapshotRefreshError,
)


def _grant(
    label: str = "initial",
    *,
    access_expires_in: timedelta = timedelta(minutes=30),
    refresh_expires_in: timedelta = timedelta(days=7),
) -> SchwabTokenGrant:
    now = datetime.now(UTC)
    bearer_type = "Bearer"
    return SchwabTokenGrant(
        access_token=f"synthetic-{label}-access-credential",
        refresh_token=f"synthetic-{label}-refresh-credential",
        token_type=bearer_type,
        expires_at=now + access_expires_in,
        refresh_expires_at=now + refresh_expires_in,
        scopes=("api",),
    )


def _connected(
    settings: Settings,
    *,
    account_count: int = 1,
) -> tuple[Database, ProviderConnectionRepository, str, tuple[str, ...]]:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    database.initialize()
    portfolio = PortfolioRepository(database)
    connections = ProviderConnectionRepository(database)
    binding = "synthetic-coordinator-session-binding"
    connection, state = connections.start_oauth(binding)
    attempt = connections.consume_oauth(state, binding)
    connections.save_token_grant(attempt, _grant())
    hashes: list[str] = []
    for index in range(account_count):
        account = portfolio.create_account(
            name=f"Synthetic coordinator account {index}",
            account_type="roth_ira" if index == 0 else "hsa",
            tax_treatment="tax_free" if index == 0 else "tax_advantaged",
        )
        provider_hash = f"synthetic-coordinator-account-hash-{index}"
        connections.map_account(
            connection.id,
            account.id,
            external_account_hash=provider_hash,
        )
        hashes.append(provider_hash)
    return database, connections, connection.id, tuple(hashes)


def _snapshot(provider_hash: str) -> SchwabAccountSnapshot:
    return SchwabAccountSnapshot(
        provider_account_hash=provider_hash,
        observed_at_utc=datetime.now(UTC),
        account_type=None,
        liquidation_value=Decimal("25.00000000"),
        cash_balance=Decimal("25.00000000"),
        positions=(),
    )


class _OAuthClient:
    def __init__(self, result: SchwabTokenGrant | Exception | None = None) -> None:
        self.result = result or _grant("refreshed")
        self.refresh_tokens: list[str] = []

    def authorization_url(self, *, state: str) -> str:
        raise AssertionError("authorization is outside the snapshot coordinator")

    async def exchange_authorization_code(self, *, code: str) -> SchwabTokenGrant:
        raise AssertionError("authorization is outside the snapshot coordinator")

    async def refresh_access(self, *, refresh_token: str) -> SchwabTokenGrant:
        self.refresh_tokens.append(refresh_token)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _ReadClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def account_snapshot(
        self, *, access_token: str, provider_account_hash: str
    ) -> SchwabAccountSnapshot:
        self.calls.append((access_token, provider_account_hash))
        return _snapshot(provider_account_hash)

    async def linked_accounts(self, *, access_token: str):
        raise AssertionError("account discovery is outside the snapshot coordinator")

    async def transactions(self, **_: object):
        raise AssertionError("transactions are outside the snapshot coordinator")

    async def quote(self, **_: object):
        raise AssertionError("quotes are outside the snapshot coordinator")


def _coordinator(
    database: Database,
    connections: ProviderConnectionRepository,
    oauth: _OAuthClient,
    read: _ReadClient,
) -> SchwabSnapshotCoordinator:
    return SchwabSnapshotCoordinator(
        connections=connections,
        oauth_client=oauth,
        read_client=read,
        snapshots=SchwabSnapshotService(database, connections),
    )


def test_coordinator_is_constructed_but_has_no_invocation_route(settings: Settings) -> None:
    app = create_app(
        settings,
        schwab_oauth_client=_OAuthClient(),
        schwab_read_client=_ReadClient(),
    )

    assert isinstance(app.state.schwab_snapshot_coordinator, SchwabSnapshotCoordinator)
    paths = {route.path for route in app.routes}
    assert not any("snapshot" in path or "refresh" in path for path in paths)


def test_http_provenance_migration_preserves_existing_synthetic_snapshot(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(database_module, "LATEST_SCHEMA_VERSION", 8)
    database, connections, connection_id, hashes = _connected(settings)
    service = SchwabSnapshotService(database, connections)

    async def synthetic_load(provider_hash: str) -> SchwabAccountSnapshot:
        return _snapshot(provider_hash)

    prior = asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor="synthetic-coordinator@example.invalid",
            load_snapshot=synthetic_load,
        )
    )
    monkeypatch.setattr(database_module, "LATEST_SCHEMA_VERSION", 9)
    database.initialize()

    assert service.current(connection_id).id == prior.id  # type: ignore[union-attr]
    current = asyncio.run(
        _coordinator(database, connections, _OAuthClient(), _ReadClient()).refresh(
            connection_id=connection_id,
            actor="synthetic-coordinator@example.invalid",
        )
    )
    assert current.provenance == SCHWAB_HTTP_SNAPSHOT_PROVENANCE
    assert hashes == ("synthetic-coordinator-account-hash-0",)
    with closing(database.connect()) as conn:
        provenances = {
            str(row[0])
            for row in conn.execute(
                "SELECT provenance FROM provider_portfolio_snapshots ORDER BY sync_run_id"
            ).fetchall()
        }
    assert provenances == {"schwab_synthetic_read_only_v1", SCHWAB_HTTP_SNAPSHOT_PROVENANCE}


def test_coordinator_loads_encrypted_grant_and_only_current_mapped_accounts(
    settings: Settings,
) -> None:
    database, connections, connection_id, hashes = _connected(settings, account_count=2)
    oauth = _OAuthClient()
    read = _ReadClient()

    publication = asyncio.run(
        _coordinator(database, connections, oauth, read).refresh(
            connection_id=connection_id,
            actor="synthetic-coordinator@example.invalid",
        )
    )

    assert oauth.refresh_tokens == []
    assert {provider_hash for _, provider_hash in read.calls} == set(hashes)
    assert {access_token for access_token, _ in read.calls} == {_grant().access_token}
    assert publication.mapped_account_count == 2
    assert publication.provenance == SCHWAB_HTTP_SNAPSHOT_PROVENANCE
    with closing(database.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM provider_portfolio_snapshots").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM provider_balance_snapshots").fetchone()[0] == 2


def test_atomic_refresh_race_keeps_one_rotated_grant(settings: Settings) -> None:
    database, connections, connection_id, _ = _connected(settings)
    expired = _grant("expired", access_expires_in=timedelta(minutes=-1))
    connections.replace_token_grant(connection_id, expired)
    both_entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    class RacingOAuth(_OAuthClient):
        async def refresh_access(self, *, refresh_token: str) -> SchwabTokenGrant:
            nonlocal calls
            calls += 1
            call_number = calls
            if calls == 2:
                both_entered.set()
            await both_entered.wait()
            await release.wait()
            return _grant(f"race-{call_number}")

    async def race() -> tuple[str, str]:
        oauth = RacingOAuth()
        first = _coordinator(database, connections, oauth, _ReadClient())
        second = _coordinator(database, connections, oauth, _ReadClient())
        tasks = (
            asyncio.create_task(first._usable_access_token(connection_id)),
            asyncio.create_task(second._usable_access_token(connection_id)),
        )
        await both_entered.wait()
        release.set()
        return tuple(await asyncio.gather(*tasks))  # type: ignore[return-value]

    returned = asyncio.run(race())
    stored = connections.token_grant(connection_id)

    assert stored is not None
    assert calls == 2
    assert returned == (stored.access_token, stored.access_token)
    assert stored.refresh_token in {
        "synthetic-race-1-refresh-credential",
        "synthetic-race-2-refresh-credential",
    }


@pytest.mark.parametrize(
    "oauth_error",
    [
        SchwabOAuthError("provider_timeout"),
        SchwabOAuthError("provider_authorization_rejected"),
    ],
    ids=("timeout", "rejected-or-revoked"),
)
def test_failed_batch_token_acquisition_is_single_flight_and_preserves_prior_snapshot(
    settings: Settings,
    oauth_error: SchwabOAuthError,
) -> None:
    database, connections, connection_id, _ = _connected(settings, account_count=3)
    prior = asyncio.run(
        _coordinator(database, connections, _OAuthClient(), _ReadClient()).refresh(
            connection_id=connection_id,
            actor="synthetic-coordinator@example.invalid",
        )
    )
    expired = _grant("single-flight-failure", access_expires_in=timedelta(minutes=-1))
    connections.replace_token_grant(connection_id, expired)
    oauth = _OAuthClient(oauth_error)
    read = _ReadClient()

    with pytest.raises(SnapshotRefreshError, match=oauth_error.code):
        asyncio.run(
            _coordinator(database, connections, oauth, read).refresh(
                connection_id=connection_id,
                actor="synthetic-coordinator@example.invalid",
            )
        )

    assert oauth.refresh_tokens == [expired.refresh_token]
    assert read.calls == []
    assert SchwabSnapshotService(database, connections).current(connection_id).id == prior.id  # type: ignore[union-attr]
    with closing(database.connect()) as conn:
        run = conn.execute("SELECT status, error_code FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
        assert tuple(run) == ("failed", oauth_error.code)


def test_every_loader_observes_the_same_failed_token_acquisition(settings: Settings) -> None:
    database, connections, connection_id, hashes = _connected(settings, account_count=3)
    expired = _grant("shared-failure", access_expires_in=timedelta(minutes=-1))
    connections.replace_token_grant(connection_id, expired)
    oauth = _OAuthClient(SchwabOAuthError("provider_timeout"))
    read = _ReadClient()

    class ObservingSnapshots:
        results: tuple[object, ...] = ()

        async def refresh(self, *, load_snapshot, **_: object):
            self.results = tuple(
                await asyncio.gather(
                    *(load_snapshot(provider_hash) for provider_hash in hashes),
                    return_exceptions=True,
                )
            )
            raise self.results[0]

    observing = ObservingSnapshots()
    coordinator = SchwabSnapshotCoordinator(
        connections=connections,
        oauth_client=oauth,
        read_client=read,
        snapshots=observing,  # type: ignore[arg-type]
    )

    with pytest.raises(SchwabOAuthError, match="provider_timeout"):
        asyncio.run(
            coordinator.refresh(
                connection_id=connection_id,
                actor="synthetic-coordinator@example.invalid",
            )
        )

    assert oauth.refresh_tokens == [expired.refresh_token]
    assert read.calls == []
    assert len(observing.results) == 3
    assert all(isinstance(result, SchwabOAuthError) for result in observing.results)
    assert len({id(result) for result in observing.results}) == 1


def test_successful_batch_token_refresh_is_single_flight_for_all_mapped_accounts(
    settings: Settings,
) -> None:
    database, connections, connection_id, hashes = _connected(settings, account_count=3)
    expired = _grant("single-flight-success", access_expires_in=timedelta(minutes=-1))
    connections.replace_token_grant(connection_id, expired)
    refreshed = _grant("single-flight-winner")
    oauth = _OAuthClient(refreshed)
    read = _ReadClient()

    publication = asyncio.run(
        _coordinator(database, connections, oauth, read).refresh(
            connection_id=connection_id,
            actor="synthetic-coordinator@example.invalid",
        )
    )

    assert oauth.refresh_tokens == [expired.refresh_token]
    assert {provider_hash for _, provider_hash in read.calls} == set(hashes)
    assert {access_token for access_token, _ in read.calls} == {refreshed.access_token}
    assert publication.mapped_account_count == 3


@pytest.mark.parametrize(
    "category",
    [
        SchwabValidationCategory.INSTRUMENT_STRUCTURE,
        SchwabValidationCategory.INSTRUMENT_IDENTITY,
        SchwabValidationCategory.INSTRUMENT_METADATA,
        SchwabValidationCategory.INSTRUMENT_SUBTYPE_MALFORMED,
        SchwabValidationCategory.INSTRUMENT_SUBTYPE_UNSUPPORTED,
        SchwabValidationCategory.INSTRUMENT_TYPE_MALFORMED,
        SchwabValidationCategory.INSTRUMENT_TYPE_UNSUPPORTED,
    ],
)
def test_rotated_refresh_token_survives_downstream_parser_failure(
    settings: Settings,
    category: SchwabValidationCategory,
) -> None:
    database, connections, connection_id, _ = _connected(settings, account_count=3)
    prior = asyncio.run(
        _coordinator(database, connections, _OAuthClient(), _ReadClient()).refresh(
            connection_id=connection_id,
            actor="synthetic-coordinator@example.invalid",
        )
    )
    expired = _grant("stale-before-parser-failure", access_expires_in=timedelta(minutes=-1))
    rotated = _grant("rotated-before-parser-failure")
    connections.replace_token_grant(connection_id, expired)
    oauth = _OAuthClient(rotated)

    class ParserFailureRead(_ReadClient):
        async def account_snapshot(
            self, *, access_token: str, provider_account_hash: str
        ) -> SchwabAccountSnapshot:
            self.calls.append((access_token, provider_account_hash))
            raise SchwabValidationError(category)

    read = ParserFailureRead()
    expected_error = f"provider_validation_{category.value}"
    with pytest.raises(SnapshotRefreshError, match=expected_error):
        asyncio.run(
            _coordinator(database, connections, oauth, read).refresh(
                connection_id=connection_id,
                actor="synthetic-coordinator@example.invalid",
            )
        )

    stored = connections.token_grant(connection_id)
    assert stored is not None
    assert oauth.refresh_tokens == [expired.refresh_token]
    assert stored.refresh_token == rotated.refresh_token
    assert stored.refresh_token != expired.refresh_token
    assert len({provider_hash for _, provider_hash in read.calls}) == len(read.calls)
    assert SchwabSnapshotService(database, connections).current(connection_id).id == prior.id  # type: ignore[union-attr]
    with closing(database.connect()) as conn:
        run = conn.execute(
            "SELECT status, records_written, error_code FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert tuple(run) == ("failed", 0, expected_error)


def test_batch_cancellation_drains_shared_token_acquisition(settings: Settings) -> None:
    database, connections, connection_id, _ = _connected(settings, account_count=3)
    expired = _grant("cancelled-acquisition", access_expires_in=timedelta(minutes=-1))
    connections.replace_token_grant(connection_id, expired)
    refresh_started = asyncio.Event()
    refresh_cancelled = asyncio.Event()

    class BlockingOAuth(_OAuthClient):
        async def refresh_access(self, *, refresh_token: str) -> SchwabTokenGrant:
            self.refresh_tokens.append(refresh_token)
            refresh_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                refresh_cancelled.set()
                raise

    async def cancel_batch() -> None:
        oauth = BlockingOAuth()
        read = _ReadClient()
        task = asyncio.create_task(
            _coordinator(database, connections, oauth, read).refresh(
                connection_id=connection_id,
                actor="synthetic-coordinator@example.invalid",
            )
        )
        await refresh_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert oauth.refresh_tokens == [expired.refresh_token]
        assert read.calls == []
        assert refresh_cancelled.is_set()
        assert not [
            pending
            for pending in asyncio.all_tasks()
            if pending is not asyncio.current_task() and not pending.done()
        ]

    asyncio.run(cancel_batch())
    with closing(database.connect()) as conn:
        run = conn.execute("SELECT status, error_code FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(run) == ("failed", "refresh_cancelled")


@pytest.mark.parametrize("revoked", [False, True])
def test_expired_or_revoked_grant_preserves_prior_snapshot(
    settings: Settings,
    revoked: bool,
) -> None:
    database, connections, connection_id, _ = _connected(settings)
    baseline = _coordinator(database, connections, _OAuthClient(), _ReadClient())
    prior = asyncio.run(
        baseline.refresh(connection_id=connection_id, actor="synthetic-coordinator@example.invalid")
    )
    grant = _grant(
        "unusable",
        access_expires_in=timedelta(minutes=-1),
        refresh_expires_in=timedelta(minutes=5 if revoked else -1),
    )
    connections.replace_token_grant(connection_id, grant)
    oauth = _OAuthClient(SchwabOAuthError("provider_authorization_rejected"))
    read = _ReadClient()

    with pytest.raises(SnapshotRefreshError, match="provider_authorization_rejected"):
        asyncio.run(
            _coordinator(database, connections, oauth, read).refresh(
                connection_id=connection_id,
                actor="synthetic-coordinator@example.invalid",
            )
        )

    assert read.calls == []
    assert oauth.refresh_tokens == ([grant.refresh_token] if revoked else [])
    assert SchwabSnapshotService(database, connections).current(connection_id).id == prior.id  # type: ignore[union-attr]


def test_partial_account_failure_cancels_sibling_and_preserves_publication(
    settings: Settings,
) -> None:
    database, connections, connection_id, hashes = _connected(settings, account_count=2)
    prior = asyncio.run(
        _coordinator(database, connections, _OAuthClient(), _ReadClient()).refresh(
            connection_id=connection_id,
            actor="synthetic-coordinator@example.invalid",
        )
    )
    sibling_cancelled = asyncio.Event()

    class PartialRead(_ReadClient):
        async def account_snapshot(
            self, *, access_token: str, provider_account_hash: str
        ) -> SchwabAccountSnapshot:
            if provider_account_hash == hashes[0]:
                await asyncio.sleep(0)
                raise SchwabOAuthError("provider_unavailable")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise

    with pytest.raises(SnapshotRefreshError, match="provider_unavailable"):
        asyncio.run(
            _coordinator(database, connections, _OAuthClient(), PartialRead()).refresh(
                connection_id=connection_id,
                actor="synthetic-coordinator@example.invalid",
            )
        )

    assert sibling_cancelled.is_set()
    assert SchwabSnapshotService(database, connections).current(connection_id).id == prior.id  # type: ignore[union-attr]
    with closing(database.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM provider_portfolio_snapshots").fetchone()[0] == 1


def test_cancellation_and_mapping_change_preserve_prior_snapshot(settings: Settings) -> None:
    database, connections, connection_id, _ = _connected(settings)
    prior = asyncio.run(
        _coordinator(database, connections, _OAuthClient(), _ReadClient()).refresh(
            connection_id=connection_id,
            actor="synthetic-coordinator@example.invalid",
        )
    )

    class BlockingRead(_ReadClient):
        async def account_snapshot(
            self, *, access_token: str, provider_account_hash: str
        ) -> SchwabAccountSnapshot:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def cancel() -> None:
        task = asyncio.create_task(
            _coordinator(database, connections, _OAuthClient(), BlockingRead()).refresh(
                connection_id=connection_id,
                actor="synthetic-coordinator@example.invalid",
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    assert SchwabSnapshotService(database, connections).current(connection_id).id == prior.id  # type: ignore[union-attr]

    with closing(database.connect()) as conn:
        mapping_id = str(
            conn.execute(
                "SELECT id FROM provider_account_mappings WHERE connection_id=?", (connection_id,)
            ).fetchone()[0]
        )

    class MappingChangeRead(_ReadClient):
        async def account_snapshot(
            self, *, access_token: str, provider_account_hash: str
        ) -> SchwabAccountSnapshot:
            with database.transaction() as conn:
                conn.execute(
                    "UPDATE provider_account_mappings SET status='archived' WHERE id=?",
                    (mapping_id,),
                )
            return _snapshot(provider_account_hash)

    with pytest.raises(SnapshotRefreshError, match="snapshot_mapping_changed"):
        asyncio.run(
            _coordinator(database, connections, _OAuthClient(), MappingChangeRead()).refresh(
                connection_id=connection_id,
                actor="synthetic-coordinator@example.invalid",
            )
        )
    assert SchwabSnapshotService(database, connections).current(connection_id).id == prior.id  # type: ignore[union-attr]


def _operator(
    settings: Settings,
    database: Database,
    connections: ProviderConnectionRepository,
    oauth: _OAuthClient,
    read: _ReadClient,
    *,
    compatibility: bool = True,
    recovery: bool = True,
    recovery_check: Callable[[datetime], bool] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> LiveSnapshotOperator:
    operations = settings.database_path.parent / "operations"
    operations.mkdir(mode=0o700, exist_ok=True)
    operations.chmod(0o700)
    return LiveSnapshotOperator(
        database=database,
        connections=connections,
        oauth_client=oauth,
        read_client=read,
        operations_dir=operations,
        deployed_commit="a" * 40,
        compatibility_check=lambda: compatibility,
        recovery_check=recovery_check or (lambda _: recovery),
        clock=clock,
    )


def test_live_operator_authorization_is_short_lived_owner_only_and_single_use(
    settings: Settings,
) -> None:
    operations = settings.database_path.parent / "operations"
    operations.mkdir(mode=0o700, parents=True)
    store = LiveSnapshotAuthorizationStore(operations)
    now = datetime.now(UTC)

    store.create(3, now=now)
    assert store.path.stat().st_mode & 0o077 == 0
    authorization = store.consume(now=now + timedelta(minutes=1))
    assert authorization.expected_mapping_count == 3
    assert not store.path.exists()
    with pytest.raises(LiveSnapshotOperatorError, match="operator_authorization_invalid"):
        store.consume(now=now + timedelta(minutes=1))

    store.create(3, now=now)
    with pytest.raises(LiveSnapshotOperatorError, match="operator_authorization_invalid"):
        store.consume(now=now + timedelta(minutes=16))
    assert not store.path.exists()


@pytest.mark.parametrize("failed_gate", ["compatibility", "recovery", "mapping_count"])
def test_live_operator_fails_preconditions_before_token_or_network(
    settings: Settings,
    failed_gate: str,
) -> None:
    database, connections, _, _ = _connected(settings, account_count=2)
    oauth = _OAuthClient()
    read = _ReadClient()
    operator = _operator(
        settings,
        database,
        connections,
        oauth,
        read,
        compatibility=failed_gate != "compatibility",
        recovery=failed_gate != "recovery",
    )
    LiveSnapshotAuthorizationStore(operator.operations_dir).create(3 if failed_gate == "mapping_count" else 2)

    result = asyncio.run(operator.run())

    assert result.atomic_publication == "not_published"
    assert result.reconciliation == "fail"
    assert result.refresh_count == 0
    assert result.account_read_count == 0
    assert oauth.refresh_tokens == []
    assert read.calls == []
    if failed_gate in {"compatibility", "recovery", "mapping_count"}:
        with closing(database.connect()) as conn:
            run = conn.execute("SELECT status, error_code FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
        assert tuple(run) == ("failed", "operator_precondition_failed")


def test_live_operator_script_is_absent_from_automatic_and_application_paths() -> None:
    project_root = Path(__file__).resolve().parents[1]
    automatic_paths = (
        project_root / "app.py",
        project_root / "northstar/main.py",
        project_root / "northstar/operations/jobs.py",
        project_root / "docker-compose.yml",
        project_root / "deployment/mac-mini/com.northstar.local.backup.plist",
        project_root / "deployment/mac-mini/com.northstar.local.recovery.plist",
    )

    assert all("run_schwab_live_snapshot" not in path.read_text(encoding="utf-8") for path in automatic_paths)


def test_live_operator_executes_once_with_privacy_safe_result_and_no_disallowed_endpoint(
    settings: Settings,
) -> None:
    database, connections, _, hashes = _connected(settings, account_count=3)
    oauth = _OAuthClient()
    read = _ReadClient()
    operator = _operator(settings, database, connections, oauth, read)
    store = LiveSnapshotAuthorizationStore(operator.operations_dir)
    store.create(3)

    result = asyncio.run(operator.run())

    assert result.expected_mapping_count == result.observed_mapping_count == 3
    assert result.refresh_count == 0
    assert result.account_read_count == 3
    assert result.atomic_publication == "published"
    assert result.reconciliation == "pass"
    assert result.no_disallowed_endpoint_called
    assert set(result.safe_dict()) == {
        "deployed_commit",
        "execution_timestamp",
        "expected_mapping_count",
        "observed_mapping_count",
        "refresh_count",
        "account_read_count",
        "atomic_publication",
        "reconciliation",
        "no_disallowed_endpoint_called",
    }
    serialized = str(result.safe_dict())
    assert not any(provider_hash in serialized for provider_hash in hashes)
    assert "credential" not in serialized

    replay = asyncio.run(operator.run())
    assert replay.atomic_publication == "not_published"
    assert len(read.calls) == 3


def test_live_operator_rejects_mapping_change_before_coordinator_load_without_network(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, connections, connection_id, _ = _connected(settings)
    oauth = _OAuthClient()
    read = _ReadClient()
    operator = _operator(settings, database, connections, oauth, read)
    LiveSnapshotAuthorizationStore(operator.operations_dir).create(1)
    original_refresh = SchwabSnapshotCoordinator.refresh
    replacement_hash = "synthetic-unauthorized-replacement-hash"

    async def change_mapping_before_load(self, **kwargs):
        with database.transaction() as conn:
            conn.execute(
                """UPDATE provider_account_mappings SET status='archived', updated_at=?
                   WHERE connection_id=?""",
                (datetime.now(UTC).isoformat(), connection_id),
            )
        replacement = PortfolioRepository(database).create_account(
            name="Synthetic replacement account",
            account_type="taxable_brokerage",
            tax_treatment="taxable",
        )
        connections.map_account(
            connection_id,
            replacement.id,
            external_account_hash=replacement_hash,
        )
        return await original_refresh(self, **kwargs)

    monkeypatch.setattr(SchwabSnapshotCoordinator, "refresh", change_mapping_before_load)

    result = asyncio.run(operator.run())

    assert result.observed_mapping_count == 1
    assert result.refresh_count == result.account_read_count == 0
    assert result.atomic_publication == "not_published"
    assert oauth.refresh_tokens == []
    assert read.calls == []
    assert not any(provider_hash == replacement_hash for _, provider_hash in read.calls)
    assert SchwabSnapshotService(database, connections).current(connection_id) is None


def test_live_operator_rechecks_authorization_after_recovery_before_provider_work(
    settings: Settings,
) -> None:
    database, connections, _, _ = _connected(settings)
    oauth = _OAuthClient()
    read = _ReadClient()
    started_at = datetime(2026, 8, 16, 12, tzinfo=UTC)
    current = [started_at]

    def complete_slow_recovery(_: datetime) -> bool:
        current[0] = started_at + timedelta(minutes=16)
        return True

    operator = _operator(
        settings,
        database,
        connections,
        oauth,
        read,
        recovery_check=complete_slow_recovery,
        clock=lambda: current[0],
    )
    LiveSnapshotAuthorizationStore(operator.operations_dir).create(1, now=started_at)

    result = asyncio.run(operator.run())

    assert result.observed_mapping_count == 1
    assert result.refresh_count == result.account_read_count == 0
    assert result.atomic_publication == "not_published"
    assert oauth.refresh_tokens == []
    assert read.calls == []


def test_live_operator_allows_one_refresh_and_never_retries_failed_account_read(
    settings: Settings,
) -> None:
    database, connections, connection_id, _ = _connected(settings, account_count=3)
    prior = asyncio.run(
        _coordinator(database, connections, _OAuthClient(), _ReadClient()).refresh(
            connection_id=connection_id,
            actor="synthetic-coordinator@example.invalid",
        )
    )
    expired = _grant("operator-expired", access_expires_in=timedelta(minutes=-1))
    connections.replace_token_grant(connection_id, expired)
    oauth = _OAuthClient(_grant("operator-refreshed"))

    class FailedRead(_ReadClient):
        async def account_snapshot(
            self, *, access_token: str, provider_account_hash: str
        ) -> SchwabAccountSnapshot:
            self.calls.append((access_token, provider_account_hash))
            raise SchwabOAuthError("provider_unavailable")

    read = FailedRead()
    operator = _operator(settings, database, connections, oauth, read)
    LiveSnapshotAuthorizationStore(operator.operations_dir).create(3)

    result = asyncio.run(operator.run())

    assert result.refresh_count == 1
    assert oauth.refresh_tokens == [expired.refresh_token]
    assert 1 <= result.account_read_count <= 3
    assert len({provider_hash for _, provider_hash in read.calls}) == len(read.calls)
    assert result.atomic_publication == "not_published"
    assert SchwabSnapshotService(database, connections).current(connection_id).id == prior.id  # type: ignore[union-attr]


def test_live_operator_cross_process_lock_consumes_no_authorization_or_network(
    settings: Settings,
) -> None:
    database, connections, _, _ = _connected(settings)
    oauth = _OAuthClient()
    read = _ReadClient()
    operator = _operator(settings, database, connections, oauth, read)
    store = LiveSnapshotAuthorizationStore(operator.operations_dir)
    store.create(1)

    with ExclusiveJobLock(operator.operations_dir, "schwab-live-snapshot"):
        result = asyncio.run(operator.run())

    assert result.account_read_count == result.refresh_count == 0
    assert store.path.exists()
    assert read.calls == []
