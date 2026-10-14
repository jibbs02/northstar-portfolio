from __future__ import annotations

import asyncio
import base64
import copy
import uuid
from contextlib import closing
from dataclasses import fields, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from northstar.config import Settings
from northstar.data import Database, PortfolioRepository, ProviderConnectionRepository
from northstar.data.portfolio import encrypted_json
from northstar.integrations import (
    HttpSchwabOAuthClient,
    HttpSchwabReadClient,
    SchwabAccountReference,
    SchwabAccountSnapshot,
    SchwabOAuthClient,
    SchwabOAuthError,
    SchwabPosition,
    SchwabReadClient,
    SchwabTokenGrant,
    SchwabValidationError,
    parse_synthetic_account_snapshot,
)
from northstar.integrations.schwab import (
    MAX_DISCOVERY_RESPONSE_BYTES,
    MAX_OAUTH_RESPONSE_BYTES,
    MAX_SNAPSHOT_RESPONSE_BYTES,
    MAX_TRANSACTION_ITEMS,
    MAX_TRANSACTION_RESPONSE_BYTES,
    MAX_TRANSACTIONS,
    SCHWAB_AUTHORIZE_URL,
    SCHWAB_TOKEN_URL,
    SCHWAB_TRANSACTION_TYPES,
)
from northstar.services import (
    BackupService,
    SchwabSnapshotCoordinator,
    SchwabSnapshotService,
    SnapshotRefreshError,
)
from northstar.services.schwab_discovery import DiscoveryError, SchwabDiscoveryService


def token_grant() -> SchwabTokenGrant:
    now = datetime.now(UTC)
    synthetic_access = "synthetic-placeholder-access-credential"
    synthetic_refresh = "synthetic-placeholder-refresh-credential"
    bearer_type = "Bearer"
    return SchwabTokenGrant(
        access_token=synthetic_access,
        refresh_token=synthetic_refresh,
        token_type=bearer_type,
        expires_at=now + timedelta(minutes=30),
        refresh_expires_at=now + timedelta(days=7),
        scopes=("synthetic-read",),
    )


def connected_repository(
    settings: Settings,
) -> tuple[Database, PortfolioRepository, ProviderConnectionRepository, str]:
    database = Database(settings.database_path, settings.data_vault(), settings.database_key)
    database.initialize()
    portfolio = PortfolioRepository(database)
    connections = ProviderConnectionRepository(database)
    session_binding = "synthetic-placeholder-oauth-session-binding"
    connection, state = connections.start_oauth(session_binding)
    attempt = connections.consume_oauth(state, session_binding)
    connections.save_token_grant(attempt, token_grant())
    return database, portfolio, connections, connection.id


def test_schwab_configuration_is_opt_in_and_requires_an_exact_callback(settings: Settings) -> None:
    settings.validate()
    synthetic_secret = "-".join(("synthetic", "placeholder", "client", "secret"))
    enabled = replace(
        settings,
        schwab_enabled=True,
        schwab_client_id="synthetic-placeholder-client-id",
        schwab_client_secret=synthetic_secret,
        schwab_redirect_uri="http://127.0.0.1:8010/integrations/schwab/callback",
    )
    enabled.validate()
    assert synthetic_secret not in repr(enabled)
    assert "synthetic-placeholder-client-id" not in repr(enabled)

    with pytest.raises(ValueError, match="redirect URI"):
        replace(enabled, schwab_redirect_uri="https://example.com/wrong").validate()
    with pytest.raises(ValueError, match="client secret"):
        replace(enabled, schwab_client_secret=None).validate()


def test_production_schwab_callback_is_fixed_to_the_protected_origin(settings: Settings) -> None:
    synthetic_secret = "-".join(("synthetic", "placeholder", "client", "secret"))
    production = replace(
        settings,
        environment="production",
        auth_mode="cloudflare",
        cf_issuer="https://synthetic.cloudflareaccess.com",
        cf_audience="synthetic-placeholder-audience",
        allowed_email="synthetic@example.com",
        secure_cookies=True,
        schwab_enabled=True,
        schwab_client_id="synthetic-placeholder-client-id",
        schwab_client_secret=synthetic_secret,
        schwab_redirect_uri="https://northstar.example.com/integrations/schwab/callback",
    )
    production.validate()

    with pytest.raises(ValueError, match="fixed callback"):
        replace(
            production,
            schwab_redirect_uri="https://other.example.com/integrations/schwab/callback",
        ).validate()


def test_tokens_and_provider_account_keys_are_field_encrypted(settings: Settings) -> None:
    database, portfolio, connections, connection_id = connected_repository(settings)
    account = portfolio.create_account(
        name="Synthetic connected account",
        account_type="roth_ira",
        tax_treatment="tax_free",
    )
    synthetic_hash = "synthetic-placeholder-provider-account-hash"
    mapping = connections.map_account(
        connection_id,
        account.id,
        external_account_hash=synthetic_hash,
        last_four="8181",
    )

    with closing(database.connect()) as conn:
        secure = conn.execute(
            "SELECT lookup_hash, encrypted_payload FROM secure_records WHERE category=?",
            ("provider-token-bundle",),
        ).fetchone()
        stored_mapping = conn.execute(
            """SELECT encrypted_external_account_hash, external_account_hash_index,
                      encrypted_last_four
               FROM provider_account_mappings WHERE id=?""",
            (mapping.id,),
        ).fetchone()

    assert secure[0] != connection_id
    assert secure[1].startswith(b"NSV1")
    assert b"synthetic-placeholder-access-credential" not in secure[1]
    assert stored_mapping[0].startswith(b"NSV1")
    assert stored_mapping[1] != synthetic_hash
    assert stored_mapping[2].startswith(b"NSV1")
    assert connections.mapping(mapping.id).last_four == "8181"
    restored_grant = connections.token_grant(connection_id)
    assert restored_grant is not None
    synthetic_access = "-".join(("synthetic", "placeholder", "access", "credential"))
    synthetic_refresh = "-".join(("synthetic", "placeholder", "refresh", "credential"))
    assert restored_grant.access_token == synthetic_access
    assert restored_grant.refresh_token == synthetic_refresh
    assert restored_grant.scopes == ("synthetic-read",)


def test_disconnect_removes_tokens_but_preserves_mapping_provenance(settings: Settings) -> None:
    _, portfolio, connections, connection_id = connected_repository(settings)
    account = portfolio.create_account(
        name="Synthetic taxable account",
        account_type="taxable_brokerage",
        tax_treatment="taxable",
    )
    mapping = connections.map_account(
        connection_id,
        account.id,
        external_account_hash="synthetic-placeholder-account-key-two",
    )

    disconnected = connections.disconnect(connection_id)

    assert disconnected.status == "disconnected"
    assert connections.token_grant(connection_id) is None
    assert connections.mapping(mapping.id).account_id == account.id


def test_synthetic_snapshot_publication_is_atomic_reconciled_and_privacy_safe(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, portfolio, connections, connection_id = connected_repository(settings)
    account = portfolio.create_account(
        name="Synthetic snapshot account", account_type="roth_ira", tax_treatment="tax_free"
    )
    provider_hash = "synthetic-snapshot-provider-hash"
    connections.map_account(connection_id, account.id, external_account_hash=provider_hash)
    security_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    with database.transaction() as conn:
        conn.execute(
            """INSERT INTO securities(id, instrument_type, currency, encrypted_symbol, symbol_hash,
               encrypted_name, key_version, created_at, updated_at, asset_class)
               VALUES (?, 'etf', 'USD', ?, ?, ?, ?, ?, ?, 'equity')""",
            (
                security_id,
                encrypted_json(database, "securities", security_id, "symbol", "SYNX"),
                database.vault.blind_index("security-symbol", "SYNX"),
                encrypted_json(database, "securities", security_id, "name", "Synthetic ETF"),
                database.vault.key_version,
                now.isoformat(),
                now.isoformat(),
            ),
        )
    valid = parse_synthetic_account_snapshot(
        {
            "accountHash": provider_hash,
            "observedAt": datetime.now(UTC).isoformat(),
            "accountType": "ROTH_IRA",
            "balances": {"liquidationValue": "125.00", "cashBalance": "25.00"},
            "positions": [
                {
                    "instrument": {
                        "providerKey": "synthetic-instrument-key",
                        "symbol": "SYNX",
                        "description": "Synthetic ETF",
                        "type": "etf",
                    },
                    "quantity": "2",
                    "marketValue": "100",
                    "averagePrice": "50",
                }
            ],
        },
        provider_account_hash=provider_hash,
    )

    async def load(_: str):
        return valid

    service = SchwabSnapshotService(database, connections)
    published = asyncio.run(
        service.refresh(connection_id=connection_id, actor="synthetic@example.com", load_snapshot=load)
    )
    assert published.freshness == "fresh"
    assert published.reconciliation_status == "matched"
    assert published.position_count == 1

    second = asyncio.run(
        service.refresh(connection_id=connection_id, actor="synthetic@example.com", load_snapshot=load)
    )
    assert second.id != published.id
    assert service.current(connection_id).id == second.id  # type: ignore[union-attr]

    original_blind_index = type(database.vault).blind_index

    def fail_audit(vault: object, category: str, value: str) -> str:
        if category == "actor" and value == "snapshot-service":
            raise RuntimeError("synthetic audit failure")
        return original_blind_index(vault, category, value)

    monkeypatch.setattr(type(database.vault), "blind_index", fail_audit)
    with pytest.raises(SnapshotRefreshError, match="provider_refresh_failed"):
        asyncio.run(
            service.refresh(connection_id=connection_id, actor="synthetic@example.com", load_snapshot=load)
        )
    assert service.current(connection_id).id == second.id  # type: ignore[union-attr]

    unknown_security = replace(valid, positions=(replace(valid.positions[0], symbol="UNKNOWN"),))

    async def fail(_: str):
        return unknown_security

    with pytest.raises(SnapshotRefreshError, match="provider_unknown_security"):
        asyncio.run(
            service.refresh(connection_id=connection_id, actor="synthetic@example.com", load_snapshot=fail)
        )
    assert service.current(connection_id).id == second.id  # type: ignore[union-attr]
    with closing(database.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM provider_portfolio_snapshots").fetchone()[0] == 2
        stored = conn.execute("SELECT audit_metadata_json FROM provider_portfolio_snapshots").fetchone()[0]
    assert provider_hash not in stored
    assert "synthetic-instrument-key" not in stored


def test_synthetic_snapshot_reconciles_account_and_instrument_identity(settings: Settings) -> None:
    database, portfolio, connections, connection_id = connected_repository(settings)
    account = portfolio.create_account(
        name="Synthetic identity account", account_type="roth_ira", tax_treatment="tax_free"
    )
    provider_hash = "synthetic-identity-provider-hash"
    connections.map_account(connection_id, account.id, external_account_hash=provider_hash)
    security_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    with database.transaction() as conn:
        conn.execute(
            """INSERT INTO securities(id, instrument_type, currency, encrypted_symbol, symbol_hash,
               encrypted_name, key_version, created_at, updated_at, asset_class)
               VALUES (?, 'etf', 'USD', ?, ?, ?, ?, ?, ?, 'equity')""",
            (
                security_id,
                encrypted_json(database, "securities", security_id, "symbol", "SYNX"),
                database.vault.blind_index("security-symbol", "SYNX"),
                encrypted_json(database, "securities", security_id, "name", "Synthetic ETF"),
                database.vault.key_version,
                now.isoformat(),
                now.isoformat(),
            ),
        )
    payload = {
        "accountHash": provider_hash,
        "observedAt": "2026-08-11T12:00:00Z",
        "accountType": "ROTH_IRA",
        "balances": {"liquidationValue": "125.00", "cashBalance": "25.00"},
        "positions": [
            {
                "instrument": {
                    "providerKey": "synthetic-identity-instrument",
                    "symbol": "SYNX",
                    "description": "Synthetic ETF",
                    "type": "etf",
                },
                "quantity": "2",
                "marketValue": "100",
                "averagePrice": "50",
            }
        ],
    }
    valid = parse_synthetic_account_snapshot(payload, provider_account_hash=provider_hash)
    service = SchwabSnapshotService(database, connections)

    async def account_conflict(_: str):
        return replace(valid, account_type="HSA")

    with pytest.raises(SnapshotRefreshError, match="provider_account_identity_conflict"):
        asyncio.run(
            service.refresh(
                connection_id=connection_id, actor="synthetic@example.com", load_snapshot=account_conflict
            )
        )

    async def instrument_conflict(_: str):
        return replace(valid, positions=(replace(valid.positions[0], instrument_type="bond"),))

    with pytest.raises(SnapshotRefreshError, match="provider_instrument_identity_conflict"):
        asyncio.run(
            service.refresh(
                connection_id=connection_id, actor="synthetic@example.com", load_snapshot=instrument_conflict
            )
        )

    async def provider_equity_for_local_etf(_: str):
        return replace(
            valid,
            account_type=None,
            positions=(replace(valid.positions[0], instrument_type="equity"),),
        )

    published = asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor="synthetic@example.com",
            load_snapshot=provider_equity_for_local_etf,
        )
    )
    assert published.position_count == 1


def test_synthetic_snapshot_cancels_siblings_and_finalizes_cancelled_runs(settings: Settings) -> None:
    _, portfolio, connections, connection_id = connected_repository(settings)
    first = portfolio.create_account(name="First", account_type="roth_ira", tax_treatment="tax_free")
    second = portfolio.create_account(name="Second", account_type="hsa", tax_treatment="tax_advantaged")
    connections.map_accounts(
        connection_id,
        (("synthetic-cancel-fast", first.id, None), ("synthetic-cancel-slow", second.id, None)),
    )
    service = SchwabSnapshotService(connections.database, connections)
    entered = 0
    both_entered = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def loader(provider_hash: str):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await both_entered.wait()
        if provider_hash == "synthetic-cancel-fast":
            raise SchwabOAuthError("provider_timeout")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise

    with pytest.raises(SnapshotRefreshError, match="provider_timeout"):
        asyncio.run(
            service.refresh(connection_id=connection_id, actor="synthetic@example.com", load_snapshot=loader)
        )
    assert sibling_cancelled.is_set()
    with closing(connections.database.connect()) as conn:
        row = conn.execute("SELECT status, error_code FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(row) == ("failed", "provider_timeout")

    async def external_cancellation() -> None:
        started = asyncio.Event()

        async def blocked(_: str):
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            service.refresh(connection_id=connection_id, actor="synthetic@example.com", load_snapshot=blocked)
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(external_cancellation())
    with closing(connections.database.connect()) as conn:
        row = conn.execute("SELECT status, error_code FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(row) == ("failed", "refresh_cancelled")


def test_synthetic_snapshot_revalidates_connection_and_mapping_scope(settings: Settings) -> None:
    database, portfolio, connections, connection_id = connected_repository(settings)
    account = portfolio.create_account(
        name="Synthetic race account", account_type="roth_ira", tax_treatment="tax_free"
    )
    provider_hash = "synthetic-race-provider-hash"
    connections.map_account(connection_id, account.id, external_account_hash=provider_hash)
    snapshot = parse_synthetic_account_snapshot(
        {
            "accountHash": provider_hash,
            "observedAt": "2026-08-11T12:00:00Z",
            "accountType": "ROTH_IRA",
            "balances": {"liquidationValue": "0", "cashBalance": "0"},
            "positions": [],
        },
        provider_account_hash=provider_hash,
    )
    service = SchwabSnapshotService(database, connections)

    with closing(database.connect()) as conn:
        mapping_id = conn.execute(
            "SELECT id FROM provider_account_mappings WHERE connection_id=?", (connection_id,)
        ).fetchone()["id"]

    async def archive_before_publish(_: str):
        with database.transaction() as conn:
            conn.execute("UPDATE provider_account_mappings SET status='archived' WHERE id=?", (mapping_id,))
        return snapshot

    with pytest.raises(SnapshotRefreshError, match="snapshot_mapping_changed"):
        asyncio.run(
            service.refresh(
                connection_id=connection_id,
                actor="synthetic@example.com",
                load_snapshot=archive_before_publish,
            )
        )
    with database.transaction() as conn:
        conn.execute("UPDATE provider_account_mappings SET status='active' WHERE id=?", (mapping_id,))

    async def disconnect_before_publish(_: str):
        connections.disconnect(connection_id)
        return snapshot

    with pytest.raises(SnapshotRefreshError, match="snapshot_connection_changed"):
        asyncio.run(
            service.refresh(
                connection_id=connection_id,
                actor="synthetic@example.com",
                load_snapshot=disconnect_before_publish,
            )
        )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"accountHash": "synthetic-snapshot-provider-hash"},
            "provider_validation_response_envelope",
        ),
        (
            {
                "accountHash": "synthetic-snapshot-provider-hash",
                "observedAt": "2026-08-11T12:00:00Z",
                "accountType": "ROTH_IRA",
                "balances": {"liquidationValue": 100.0, "cashBalance": "0"},
                "positions": [],
            },
            "provider_validation_numeric_value",
        ),
    ],
)
def test_synthetic_snapshot_parser_fails_closed_on_incomplete_or_float_values(
    payload: object, expected: str
) -> None:
    with pytest.raises(SchwabOAuthError, match=expected):
        parse_synthetic_account_snapshot(payload, provider_account_hash="synthetic-snapshot-provider-hash")


def test_synthetic_snapshot_parser_rejects_future_and_oversized_observations() -> None:
    base = {
        "accountHash": "synthetic-snapshot-provider-hash",
        "observedAt": "2099-08-11T12:00:00Z",
        "accountType": "ROTH_IRA",
        "balances": {"liquidationValue": "0", "cashBalance": "0"},
        "positions": [],
    }
    with pytest.raises(SchwabOAuthError, match="provider_validation_response_envelope"):
        parse_synthetic_account_snapshot(base, provider_account_hash=base["accountHash"])
    oversized = dict(base, observedAt="2026-08-11T12:00:00Z", positions=[{}] * 2_001)
    with pytest.raises(SchwabOAuthError, match="provider_validation_position_structure"):
        parse_synthetic_account_snapshot(oversized, provider_account_hash=base["accountHash"])


def test_synthetic_snapshot_domain_repr_redacts_provider_and_financial_values() -> None:
    position = SchwabPosition(
        provider_instrument_key="synthetic-instrument-key",
        symbol="SYNX",
        description="Synthetic ETF",
        instrument_type="etf",
        quantity=Decimal("2"),
        market_value=Decimal("100"),
        average_price=Decimal("50"),
    )
    snapshot = SchwabAccountSnapshot(
        provider_account_hash="synthetic-account-hash",
        observed_at_utc=datetime.now(UTC),
        account_type="roth_ira",
        liquidation_value=Decimal("125"),
        cash_balance=Decimal("25"),
        positions=(position,),
    )
    rendered = repr(snapshot) + repr(position)
    assert all(
        secret not in rendered
        for secret in ("synthetic-account-hash", "synthetic-instrument-key", "SYNX", "100")
    )


def test_sync_audit_uses_blind_actor_identity_and_allowlisted_error_codes(settings: Settings) -> None:
    database, _, connections, connection_id = connected_repository(settings)
    synthetic_actor = "synthetic-user@example.com"
    sync_run = connections.start_sync(connection_id, actor=synthetic_actor)
    connections.finish_sync(
        sync_run,
        succeeded=False,
        records_seen=2,
        records_written=0,
        error_code="provider_timeout",
    )

    with closing(database.connect()) as conn:
        row = conn.execute("SELECT * FROM sync_runs WHERE id=?", (sync_run,)).fetchone()

    assert row["requested_by"] != synthetic_actor
    assert row["status"] == "failed"
    assert row["error_code"] == "provider_timeout"
    assert row["trigger_type"] == "manual"
    assert connections.get(connection_id).last_successful_sync_at is None

    with pytest.raises(ValueError, match="privacy-safe"):
        connections.finish_sync(
            connections.start_sync(connection_id, actor=synthetic_actor),
            succeeded=False,
            records_seen=0,
            records_written=0,
            error_code="source value: private",
        )


def test_provider_contract_cannot_represent_orders_or_full_account_numbers() -> None:
    public_methods = {
        name
        for contract in (SchwabOAuthClient, SchwabReadClient)
        for name in contract.__dict__
        if not name.startswith("_")
    }
    forbidden_fragments = ("order", "trade", "cancel", "replace", "place")

    assert not any(fragment in method for fragment in forbidden_fragments for method in public_methods)
    assert {field.name for field in fields(SchwabAccountReference)} == {
        "provider_account_hash",
        "last_four",
    }
    with pytest.raises(ValueError, match="account hash"):
        SchwabAccountReference("123456789")


def test_token_grant_redacts_credentials_and_rejects_malformed_expiry() -> None:
    grant = token_grant()
    rendered = repr(grant)

    assert grant.access_token not in rendered
    assert grant.refresh_token not in rendered
    with pytest.raises(ValueError, match="timezone"):
        replace(grant, expires_at=datetime.now())


def test_documented_authorization_url_includes_mandatory_one_time_state() -> None:
    synthetic_secret = "-".join(("synthetic", "placeholder", "client", "secret"))
    client = HttpSchwabOAuthClient(
        client_id="synthetic-placeholder-client-id",
        client_secret=synthetic_secret,
        redirect_uri="https://northstar.example.com/integrations/schwab/callback",
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )

    url = client.authorization_url(state="synthetic-one-time-state")
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == SCHWAB_AUTHORIZE_URL
    assert query == {
        "client_id": ["synthetic-placeholder-client-id"],
        "redirect_uri": ["https://northstar.example.com/integrations/schwab/callback"],
        "state": ["synthetic-one-time-state"],
    }
    assert synthetic_secret not in url
    asyncio.run(client.close())


def test_documented_token_exchange_uses_basic_auth_and_discards_id_token() -> None:
    synthetic_client_id = "synthetic-placeholder-client-id"
    synthetic_secret = "-".join(("synthetic", "placeholder", "client", "secret"))
    synthetic_access = "-".join(("synthetic", "placeholder", "access", "credential"))
    synthetic_refresh = "-".join(("synthetic", "placeholder", "refresh", "credential"))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "expires_in": 1800,
                "token_type": "Bearer",
                "scope": "api",
                "refresh_token": synthetic_refresh,
                "access_token": synthetic_access,
                "id_token": "synthetic-placeholder-discarded-id-token",
            },
        )

    client = HttpSchwabOAuthClient(
        client_id=synthetic_client_id,
        client_secret=synthetic_secret,
        redirect_uri="https://northstar.example.com/integrations/schwab/callback",
        transport=httpx.MockTransport(handler),
    )
    before = datetime.now(UTC)
    grant = asyncio.run(client.exchange_authorization_code(code="synthetic-decoded-authorization-code@"))
    asyncio.run(client.close())

    assert len(requests) == 1
    request = requests[0]
    expected_basic = base64.b64encode(f"{synthetic_client_id}:{synthetic_secret}".encode()).decode()
    form = parse_qs(request.content.decode())
    assert str(request.url) == SCHWAB_TOKEN_URL
    assert request.headers["Authorization"] == f"Basic {expected_basic}"
    assert request.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
    assert form == {
        "grant_type": ["authorization_code"],
        "code": ["synthetic-decoded-authorization-code@"],
        "redirect_uri": ["https://northstar.example.com/integrations/schwab/callback"],
    }
    assert grant.access_token == synthetic_access
    assert grant.refresh_token == synthetic_refresh
    assert grant.scopes == ("api",)
    assert timedelta(minutes=29) < grant.expires_at - before <= timedelta(minutes=31)
    assert grant.refresh_expires_at is not None
    assert timedelta(days=6, hours=23) < grant.refresh_expires_at - before <= timedelta(days=7, minutes=1)
    assert not hasattr(grant, "id_token")


def test_provider_oauth_errors_are_allowlisted_and_redacted() -> None:
    synthetic_secret = "-".join(("synthetic", "placeholder", "client", "secret"))
    synthetic_refresh = "-".join(("synthetic", "placeholder", "refresh", "credential"))
    private_provider_body = "private-provider-error-body-should-not-escape"

    async def exercise() -> None:
        client = HttpSchwabOAuthClient(
            client_id="synthetic-placeholder-client-id",
            client_secret=synthetic_secret,
            redirect_uri="https://northstar.example.com/integrations/schwab/callback",
            transport=httpx.MockTransport(lambda _: httpx.Response(401, text=private_provider_body)),
        )
        try:
            with pytest.raises(SchwabOAuthError) as captured:
                await client.refresh_access(refresh_token=synthetic_refresh)
            assert captured.value.code == "provider_authorization_rejected"
            assert private_provider_body not in str(captured.value)
            assert private_provider_body not in repr(captured.value)
        finally:
            await client.close()

    asyncio.run(exercise())

    with pytest.raises(ValueError, match="not allowed"):
        SchwabOAuthError("private-provider-body")


def test_oversized_oauth_response_is_stopped_while_streaming() -> None:
    class OversizedStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.chunks_yielded = 0
            self.closed = False

        async def __aiter__(self):
            chunks = (
                b"x" * (MAX_OAUTH_RESPONSE_BYTES // 2),
                b"y" * (MAX_OAUTH_RESPONSE_BYTES // 2 + 1),
                b"unreachable-provider-data",
            )
            for chunk in chunks:
                self.chunks_yielded += 1
                yield chunk

        async def aclose(self) -> None:
            self.closed = True

    stream = OversizedStream()
    synthetic_secret = "-".join(("synthetic", "placeholder", "client", "secret"))

    async def exercise() -> None:
        client = HttpSchwabOAuthClient(
            client_id="synthetic-placeholder-client-id",
            client_secret=synthetic_secret,
            redirect_uri="https://northstar.example.com/integrations/schwab/callback",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
        )
        try:
            with pytest.raises(SchwabOAuthError) as captured:
                await client.exchange_authorization_code(code="synthetic-authorization-code")
            assert captured.value.code == "provider_response_too_large"
        finally:
            await client.close()

    asyncio.run(exercise())
    assert stream.chunks_yielded == 2
    assert stream.closed


def test_account_discovery_accepts_only_documented_identity_pairs_and_redacts_numbers() -> None:
    requests: list[httpx.Request] = []
    access_token = token_grant().access_token

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            json=[
                {"accountNumber": "999900001111", "hashValue": "synthetic-account-hash-one"},
                {"accountNumber": "222233334444", "hashValue": "synthetic-account-hash-two"},
            ],
        )

    async def exercise() -> tuple[object, ...]:
        client = HttpSchwabReadClient(transport=httpx.MockTransport(handler))
        try:
            return await client.linked_accounts(access_token=access_token)
        finally:
            await client.close()

    references = asyncio.run(exercise())
    assert str(requests[0].url).endswith("/trader/v1/accounts/accountNumbers")
    assert requests[0].method == "GET"
    assert [reference.last_four for reference in references] == ["1111", "4444"]
    assert all("999900001111" not in repr(reference) for reference in references)


@pytest.mark.parametrize(
    "payload",
    [
        {"accountNumber": "999900001111", "hashValue": "synthetic-account-hash-one"},
        [{"accountNumber": "999900001111", "hashValue": "synthetic-account-hash-one", "type": "IRA"}],
        [{"accountNumber": "999900001111", "hashValue": "synthetic-account-hash-one"}] * 2,
        [{"accountNumber": "not-a-number", "hashValue": "synthetic-account-hash-one"}],
        [{"accountNumber": "999900001111", "hashValue": "123456789"}],
    ],
)
def test_account_discovery_rejects_untrusted_shapes_atomically(payload: object) -> None:
    access_token = token_grant().access_token

    async def exercise() -> None:
        client = HttpSchwabReadClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"content-type": "application/json"}, json=payload)
            )
        )
        try:
            with pytest.raises(SchwabOAuthError, match="provider_response_invalid"):
                await client.linked_accounts(access_token=access_token)
        finally:
            await client.close()

    asyncio.run(exercise())


def test_account_discovery_rejects_wrong_content_type_and_oversized_stream() -> None:
    access_token = token_grant().access_token

    async def exercise() -> None:
        wrong_type = HttpSchwabReadClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text="[]"))
        )
        try:
            with pytest.raises(SchwabOAuthError, match="provider_response_invalid"):
                await wrong_type.linked_accounts(access_token=access_token)
        finally:
            await wrong_type.close()

        oversized = HttpSchwabReadClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    content=b"x" * (MAX_DISCOVERY_RESPONSE_BYTES + 1),
                )
            )
        )
        try:
            with pytest.raises(SchwabOAuthError, match="provider_response_too_large"):
                await oversized.linked_accounts(access_token=access_token)
        finally:
            await oversized.close()

    asyncio.run(exercise())


def provider_account_snapshot_payload(mechanics: str = "CASH") -> dict[str, object]:
    balances = (
        {"totalCash": 25, "longNonMarginableMarketValue": 100}
        if mechanics == "CASH"
        else {"equity": 125, "longMarginValue": 100}
    )
    return {
        "securitiesAccount": {
            "accountNumber": "999900001111",
            "type": mechanics,
            "currentBalances": balances,
            "positions": [
                {
                    "longQuantity": 2,
                    "shortQuantity": 0,
                    "averagePrice": 50,
                    "marketValue": 100,
                    "instrument": {
                        "assetType": "EQUITY",
                        "cusip": "synthetic-provider-instrument-key",
                        "symbol": "SYNX",
                        "description": "Synthetic ETF",
                    },
                }
            ],
        }
    }


def provider_transaction_payload(activity_id: int = 101) -> dict[str, object]:
    return {
        "activityId": activity_id,
        "time": "2026-08-15T14:30:00.000Z",
        "description": "Synthetic private transaction description",
        "accountNumber": "999900001111",
        "type": "TRADE",
        "status": "VALID",
        "subAccount": "CASH",
        "tradeDate": "2026-08-14T14:30:00.000Z",
        "settlementDate": "2026-08-15T14:30:00.000Z",
        "netAmount": -25.5,
        "activityType": "EXECUTION",
        "transferItems": [
            {
                "instrument": {
                    "assetType": "EQUITY",
                    "cusip": "synthetic-transaction-instrument-key",
                    "symbol": "SYNX",
                    "type": "COMMON_STOCK",
                },
                "amount": 2,
                "cost": 100,
                "price": 50,
                "positionEffect": "OPENING",
                "privateProviderField": "synthetic-private-transfer",
            },
            {"amount": 1, "feeType": "COMMISSION"},
        ],
    }


def test_transaction_transport_uses_only_the_documented_bounded_endpoint() -> None:
    requests: list[httpx.Request] = []
    provider_hash = "synthetic-account-hash"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            json=[provider_transaction_payload()],
        )

    async def exercise():
        client = HttpSchwabReadClient(transport=httpx.MockTransport(handler))
        try:
            return await client.transactions(
                access_token=token_grant().access_token,
                provider_account_hash=provider_hash,
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 15),
            )
        finally:
            await client.close()

    records = asyncio.run(exercise())
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/trader/v1/accounts/synthetic-account-hash/transactions"
    assert request.url.params["startDate"] == "2026-08-01T00:00:00.000Z"
    assert request.url.params["endDate"] == "2026-08-15T23:59:59.999Z"
    assert request.url.params["types"] == ",".join(SCHWAB_TRANSACTION_TYPES)
    assert "symbol" not in request.url.params
    assert len(records) == 1
    record = records[0]
    assert record.provider_type == "TRADE"
    assert record.provider_status == "VALID"
    assert record.activity_type == "EXECUTION"
    assert record.sub_account == "CASH"
    assert record.provider_source_time_utc == datetime(2026, 8, 15, 14, 30, tzinfo=UTC)
    assert record.trade_date == datetime(2026, 8, 14, 14, 30, tzinfo=UTC)
    assert record.settlement_date == datetime(2026, 8, 15, 14, 30, tzinfo=UTC)
    assert record.net_amount == Decimal("-25.50000000")
    assert len(record.items) == 2
    assert record.items[0].instrument_type == "equity"
    assert record.items[0].amount == Decimal("2.000000000000")
    assert record.items[0].price == Decimal("50.00000000")
    assert record.items[0].position_effect == "OPENING"
    assert record.items[1].fee_type == "COMMISSION"
    rendered = repr(record)
    assert all(
        private not in rendered
        for private in (
            provider_hash,
            "999900001111",
            "Synthetic private transaction description",
            "synthetic-private-transfer",
            "synthetic-transaction-instrument-key",
            "SYNX",
            "25.5",
            "2026-08-15",
        )
    )


@pytest.mark.parametrize(
    ("start_date", "end_date"),
    [
        (date(2026, 8, 2), date(2026, 8, 1)),
        (date(2025, 8, 1), date(2026, 8, 2)),
    ],
)
def test_transaction_transport_rejects_invalid_windows_before_network(
    start_date: date, end_date: date
) -> None:
    async def exercise() -> None:
        client = HttpSchwabReadClient(
            transport=httpx.MockTransport(lambda _: (_ for _ in ()).throw(AssertionError()))
        )
        try:
            with pytest.raises(ValueError, match="date window"):
                await client.transactions(
                    access_token=token_grant().access_token,
                    provider_account_hash="synthetic-account-hash",
                    start_date=start_date,
                    end_date=end_date,
                )
        finally:
            await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "variant",
    [
        "wrong_envelope",
        "missing_field",
        "duplicate",
        "unknown_enum",
        "naive_date",
        "account_whitespace",
        "unknown_fee",
        "too_many_items",
        "bad_amount",
    ],
)
def test_transaction_transport_rejects_malformed_records_without_echo(variant: str) -> None:
    private_marker = "synthetic-private-transaction-marker"
    payload: object = [provider_transaction_payload()]
    if variant == "wrong_envelope":
        payload = {"private": private_marker}
    else:
        record = payload[0]
        assert isinstance(record, dict)
        record["private"] = private_marker
        if variant == "missing_field":
            record.pop("activityId")
        elif variant == "duplicate":
            payload.append(dict(record))
        elif variant == "unknown_enum":
            record["type"] = "SYNTHETIC_UNKNOWN"
        elif variant == "naive_date":
            record["time"] = "2026-08-15T14:30:00"
        elif variant == "account_whitespace":
            record["accountNumber"] = "9999 00001111"
        elif variant == "unknown_fee":
            items = record["transferItems"]
            assert isinstance(items, list) and isinstance(items[1], dict)
            items[1]["feeType"] = "SYNTHETIC_UNKNOWN"
        elif variant == "too_many_items":
            record["transferItems"] = [{} for _ in range(MAX_TRANSACTION_ITEMS + 1)]
        else:
            record["netAmount"] = True

    async def exercise() -> None:
        client = HttpSchwabReadClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"content-type": "application/json"}, json=payload)
            )
        )
        try:
            with pytest.raises(SchwabOAuthError) as captured:
                await client.transactions(
                    access_token=token_grant().access_token,
                    provider_account_hash="synthetic-account-hash",
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 8, 15),
                )
            assert str(captured.value) in {
                "provider_validation_response_envelope",
                "provider_validation_transaction_structure",
                "provider_validation_numeric_value",
            }
            assert private_marker not in str(captured.value)
        finally:
            await client.close()

    asyncio.run(exercise())


def test_transaction_transport_rejects_mixed_account_response() -> None:
    payload = [provider_transaction_payload(), provider_transaction_payload(102)]
    payload[1]["accountNumber"] = "999900002222"

    async def exercise() -> None:
        client = HttpSchwabReadClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"content-type": "application/json"}, json=payload)
            )
        )
        try:
            with pytest.raises(
                SchwabOAuthError, match="provider_validation_transaction_structure"
            ):
                await client.transactions(
                    access_token=token_grant().access_token,
                    provider_account_hash="synthetic-account-hash",
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 8, 15),
                )
        finally:
            await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("source_time", "start_date", "end_date"),
    [
        ("2026-07-31T23:59:59.999Z", date(2026, 8, 1), date(2026, 8, 15)),
        ("2026-08-16T00:00:00.000Z", date(2026, 8, 1), date(2026, 8, 15)),
        ("9998-08-15T14:30:00.000Z", date(9998, 8, 15), date(9998, 8, 15)),
    ],
)
def test_transaction_transport_rejects_source_time_outside_window_or_future_skew(
    source_time: str, start_date: date, end_date: date
) -> None:
    payload = provider_transaction_payload()
    payload["time"] = source_time

    async def exercise() -> None:
        client = HttpSchwabReadClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "application/json"}, json=[payload]
                )
            )
        )
        try:
            with pytest.raises(
                SchwabOAuthError, match="provider_validation_transaction_structure"
            ):
                await client.transactions(
                    access_token=token_grant().access_token,
                    provider_account_hash="synthetic-account-hash",
                    start_date=start_date,
                    end_date=end_date,
                )
        finally:
            await client.close()

    asyncio.run(exercise())


def test_transaction_transport_rejects_a_maximum_sized_possibly_truncated_page() -> None:
    payload = [provider_transaction_payload(index) for index in range(MAX_TRANSACTIONS)]

    async def exercise() -> None:
        client = HttpSchwabReadClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"content-type": "application/json"}, json=payload)
            )
        )
        try:
            with pytest.raises(SchwabOAuthError, match="provider_transaction_history_incomplete"):
                await client.transactions(
                    access_token=token_grant().access_token,
                    provider_account_hash="synthetic-account-hash",
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 8, 15),
                )
        finally:
            await client.close()

    asyncio.run(exercise())


def test_transaction_transport_rejects_wrong_content_type_oversize_timeout_and_cancellation() -> None:
    class OversizedTransactionStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * (MAX_TRANSACTION_RESPONSE_BYTES + 1)

        async def aclose(self) -> None:
            return None

    async def exercise() -> None:
        clients = (
            (
                HttpSchwabReadClient(
                    transport=httpx.MockTransport(lambda _: httpx.Response(200, text="[]"))
                ),
                "provider_validation_response_envelope",
            ),
            (
                HttpSchwabReadClient(
                    transport=httpx.MockTransport(
                        lambda _: httpx.Response(
                            200,
                            headers={"content-type": "application/json"},
                            stream=OversizedTransactionStream(),
                        )
                    )
                ),
                "provider_response_too_large",
            ),
            (
                HttpSchwabReadClient(
                    transport=httpx.MockTransport(
                        lambda _: (_ for _ in ()).throw(httpx.ReadTimeout("synthetic"))
                    )
                ),
                "provider_timeout",
            ),
        )
        try:
            for client, expected in clients:
                with pytest.raises(SchwabOAuthError, match=expected):
                    await client.transactions(
                        access_token=token_grant().access_token,
                        provider_account_hash="synthetic-account-hash",
                        start_date=date(2026, 8, 1),
                        end_date=date(2026, 8, 15),
                    )
        finally:
            for client, _ in clients:
                await client.close()

        cancelled_client = HttpSchwabReadClient(
            transport=httpx.MockTransport(lambda _: (_ for _ in ()).throw(asyncio.CancelledError()))
        )
        try:
            with pytest.raises(asyncio.CancelledError):
                await cancelled_client.transactions(
                    access_token=token_grant().access_token,
                    provider_account_hash="synthetic-account-hash",
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 8, 15),
                )
        finally:
            await cancelled_client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("mechanics", ["CASH", "MARGIN"])
def test_account_snapshot_uses_only_the_documented_mapped_account_endpoint(mechanics: str) -> None:
    requests: list[httpx.Request] = []
    access_token = token_grant().access_token
    provider_hash = "synthetic-account-hash"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            json=provider_account_snapshot_payload(mechanics),
        )

    async def exercise() -> SchwabAccountSnapshot:
        client = HttpSchwabReadClient(transport=httpx.MockTransport(handler))
        try:
            return await client.account_snapshot(
                access_token=access_token, provider_account_hash=provider_hash
            )
        finally:
            await client.close()

    snapshot = asyncio.run(exercise())
    assert str(requests[0].url) == (
        "https://api.schwabapi.com/trader/v1/accounts/synthetic-account-hash?fields=positions"
    )
    assert requests[0].method == "GET"
    assert snapshot.account_type is None
    assert snapshot.liquidation_value == Decimal("125.00000000")
    assert snapshot.cash_balance == Decimal("25.00000000")
    assert snapshot.positions[0].instrument_type == "equity"
    assert snapshot.positions[0].quantity == Decimal("2.000000000000")
    rendered = repr(snapshot) + repr(snapshot.positions[0])
    assert all(value not in rendered for value in (provider_hash, "999900001111", "SYNX", "125"))


def test_collective_etf_reconciles_only_with_a_known_local_etf(
    settings: Settings,
) -> None:
    database, portfolio, connections, connection_id = connected_repository(settings)
    account = portfolio.create_account(
        name="Synthetic collective account", account_type="roth_ira", tax_treatment="tax_free"
    )
    provider_hash = "synthetic-collective-account-hash"
    connections.map_account(connection_id, account.id, external_account_hash=provider_hash)
    security_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    with database.transaction() as conn:
        conn.execute(
            """INSERT INTO securities(id, instrument_type, currency, encrypted_symbol, symbol_hash,
               encrypted_name, key_version, created_at, updated_at, asset_class)
               VALUES (?, 'etf', 'USD', ?, ?, ?, ?, ?, ?, 'equity')""",
            (
                security_id,
                encrypted_json(database, "securities", security_id, "symbol", "SYNX"),
                database.vault.blind_index("security-symbol", "SYNX"),
                encrypted_json(database, "securities", security_id, "name", "Synthetic ETF"),
                database.vault.key_version,
                now,
                now,
            ),
        )
    payload = provider_account_snapshot_payload()
    provider_account = payload["securitiesAccount"]
    assert isinstance(provider_account, dict)
    positions = provider_account["positions"]
    assert isinstance(positions, list)
    positions[0]["instrument"]["assetType"] = "COLLECTIVE_INVESTMENT"
    positions[0]["instrument"]["type"] = "EXCHANGE_TRADED_FUND"

    async def parsed_snapshot() -> SchwabAccountSnapshot:
        client = HttpSchwabReadClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"content-type": "application/json"}, json=payload)
            )
        )
        try:
            return await client.account_snapshot(
                access_token=token_grant().access_token,
                provider_account_hash=provider_hash,
            )
        finally:
            await client.close()

    parsed = asyncio.run(parsed_snapshot())
    assert parsed.positions[0].instrument_type == "etf"
    service = SchwabSnapshotService(database, connections)

    async def compatible(_: str) -> SchwabAccountSnapshot:
        return parsed

    published = asyncio.run(
        service.refresh(
            connection_id=connection_id,
            actor="synthetic@example.com",
            load_snapshot=compatible,
            provenance="schwab_http_read_only_v1",
        )
    )

    with database.transaction() as conn:
        conn.execute(
            "UPDATE securities SET instrument_type='mutual_fund', updated_at=? WHERE id=?",
            (datetime.now(UTC).isoformat(), security_id),
        )

    async def conflict(_: str) -> SchwabAccountSnapshot:
        return parsed

    with pytest.raises(SnapshotRefreshError, match="provider_instrument_identity_conflict"):
        asyncio.run(
            service.refresh(
                connection_id=connection_id,
                actor="synthetic@example.com",
                load_snapshot=conflict,
                provenance="schwab_http_read_only_v1",
            )
        )
    assert service.current(connection_id).id == published.id  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "variant",
    [
        "additive_fields",
        "empty_positions_omitted",
        "empty_positions_null",
        "bounded_nonnumeric_account_number",
        "optional_position_fields_omitted",
        "additive_undocumented_aliases",
        "empty_cash_account",
        "empty_margin_account",
    ],
)
def test_account_snapshot_accepts_only_supported_unambiguous_variants(variant: str) -> None:
    payload = copy.deepcopy(provider_account_snapshot_payload())
    account = payload["securitiesAccount"]
    assert isinstance(account, dict)
    if variant == "additive_fields":
        payload["providerMetadata"] = {"version": "synthetic"}
        account["providerAddedField"] = "synthetic"
        positions = account["positions"]
        assert isinstance(positions, list)
        positions[0]["providerAddedField"] = "synthetic"
        positions[0]["instrument"]["providerAddedField"] = "synthetic"
    elif variant == "empty_positions_omitted":
        account.pop("positions")
        account["currentBalances"] = {"totalCash": 25, "longNonMarginableMarketValue": 0}
    elif variant == "empty_positions_null":
        account["positions"] = None
        account["currentBalances"] = {"totalCash": 25, "longNonMarginableMarketValue": 0}
    elif variant == "bounded_nonnumeric_account_number":
        account["accountNumber"] = "SYNTHETIC-ACCOUNT"
    elif variant == "optional_position_fields_omitted":
        positions = account["positions"]
        assert isinstance(positions, list)
        positions[0].pop("shortQuantity")
        positions[0]["instrument"].pop("description")
    elif variant == "additive_undocumented_aliases":
        balances = account["currentBalances"]
        assert isinstance(balances, dict)
        balances.update({"liquidationValue": 999, "cashBalance": 999})
    elif variant == "empty_cash_account":
        account["positions"] = []
        account["currentBalances"] = {"totalCash": 0, "longNonMarginableMarketValue": 0}
    elif variant == "empty_margin_account":
        account["type"] = "MARGIN"
        account["positions"] = []
        account["currentBalances"] = {"equity": 0, "longMarginValue": 0}

    async def exercise() -> SchwabAccountSnapshot:
        client = HttpSchwabReadClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"content-type": "application/json"}, json=payload)
            )
        )
        try:
            return await client.account_snapshot(
                access_token=token_grant().access_token,
                provider_account_hash="synthetic-account-hash",
            )
        finally:
            await client.close()

    snapshot = asyncio.run(exercise())
    empty_account = variant in {"empty_cash_account", "empty_margin_account"}
    expected_empty = variant.startswith("empty_positions") or empty_account
    expected_liquidation = (
        "0E-8"
        if empty_account
        else "25.00000000"
        if variant.startswith("empty_positions")
        else "125.00000000"
    )
    assert snapshot.liquidation_value == Decimal(expected_liquidation)
    assert snapshot.cash_balance == Decimal("0E-8" if empty_account else "25.00000000")
    assert len(snapshot.positions) == (0 if expected_empty else 1)


@pytest.mark.parametrize(
    ("mechanics", "balances", "expected"),
    [
        ("CASH", {}, "provider_validation_balance_variant_missing"),
        ("MARGIN", {"providerAddedField": 0}, "provider_validation_balance_variant_missing"),
        ("CASH", {"totalCash": 25}, "provider_validation_balance_variant_malformed"),
        ("CASH", {"longNonMarginableMarketValue": 100}, "provider_validation_balance_variant_malformed"),
        ("MARGIN", {"equity": 125}, "provider_validation_balance_variant_malformed"),
        ("MARGIN", {"longMarginValue": 100}, "provider_validation_balance_variant_malformed"),
        ("CASH", {"liquidationValue": 125}, "provider_validation_balance_variant_missing"),
        ("MARGIN", {"cashBalance": 25}, "provider_validation_balance_variant_missing"),
        (
            "CASH",
            {"liquidationValue": 125, "cashBalance": 25},
            "provider_validation_balance_variant_missing",
        ),
        (
            "MARGIN",
            {"liquidationValue": 125, "cashBalance": 25},
            "provider_validation_balance_variant_missing",
        ),
        (
            "CASH",
            {"totalCash": "not-a-number", "longNonMarginableMarketValue": 100},
            "provider_validation_numeric_value",
        ),
        (
            "MARGIN",
            {"equity": 125, "longMarginValue": None},
            "provider_validation_numeric_value",
        ),
    ],
)
def test_account_snapshot_separates_missing_partial_and_malformed_balance_variants(
    mechanics: str, balances: dict[str, object], expected: str
) -> None:
    payload = provider_account_snapshot_payload(mechanics)
    account = payload["securitiesAccount"]
    assert isinstance(account, dict)
    account["currentBalances"] = balances

    async def exercise() -> None:
        client = HttpSchwabReadClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"content-type": "application/json"}, json=payload)
            )
        )
        try:
            with pytest.raises(SchwabValidationError, match=expected):
                await client.account_snapshot(
                    access_token=token_grant().access_token,
                    provider_account_hash="synthetic-account-hash",
                )
        finally:
            await client.close()

    asyncio.run(exercise())


def test_validation_category_is_persisted_without_raw_response_or_logging(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    database, portfolio, connections, connection_id = connected_repository(settings)
    account = portfolio.create_account(
        name="Synthetic parser failure account",
        account_type="roth_ira",
        tax_treatment="tax_free",
    )
    provider_hash = "synthetic-parser-failure-provider-hash"
    connections.map_account(connection_id, account.id, external_account_hash=provider_hash)
    snapshots = SchwabSnapshotService(database, connections)
    previous = parse_synthetic_account_snapshot(
        {
            "accountHash": provider_hash,
            "observedAt": datetime.now(UTC).isoformat(),
            "accountType": "ROTH_IRA",
            "balances": {"liquidationValue": "25", "cashBalance": "25"},
            "positions": [],
        },
        provider_account_hash=provider_hash,
    )

    async def load_previous(_: str) -> SchwabAccountSnapshot:
        return previous

    published = asyncio.run(
        snapshots.refresh(
            connection_id=connection_id,
            actor="synthetic-actor",
            load_snapshot=load_previous,
        )
    )
    private_marker = "synthetic-private-response-marker"
    payload = {
        "securitiesAccount": {
            "accountNumber": "SYNTHETIC-ACCOUNT",
            "type": "CASH",
            "currentBalances": {"providerMessage": private_marker},
            "positions": [],
            "privateProviderField": private_marker,
        },
        "privateProviderField": private_marker,
    }
    read = HttpSchwabReadClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "application/json"}, json=payload)
        )
    )
    oauth = HttpSchwabOAuthClient(
        client_id="synthetic-placeholder-client-id",
        client_secret="-".join(("synthetic", "placeholder", "client", "secret")),
        redirect_uri="https://northstar.example.com/integrations/schwab/callback",
        transport=httpx.MockTransport(lambda _: (_ for _ in ()).throw(AssertionError())),
    )
    coordinator = SchwabSnapshotCoordinator(
        connections=connections,
        oauth_client=oauth,
        read_client=read,
        snapshots=snapshots,
    )

    async def exercise() -> None:
        try:
            with pytest.raises(
                SnapshotRefreshError, match="provider_validation_balance_variant_missing"
            ):
                await coordinator.refresh(connection_id=connection_id, actor="synthetic-actor")
        finally:
            await read.close()
            await oauth.close()

    caplog.clear()
    asyncio.run(exercise())
    with closing(database.connect()) as conn:
        run = conn.execute(
            "SELECT status, records_written, error_code FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert tuple(run) == ("failed", 0, "provider_validation_balance_variant_missing")
        assert conn.execute("SELECT COUNT(*) FROM provider_portfolio_snapshots").fetchone()[0] == 1
    assert snapshots.current(connection_id).id == published.id  # type: ignore[union-attr]
    assert private_marker.encode() not in settings.database_path.read_bytes()
    assert private_marker not in caplog.text


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, "provider_authorization_rejected"),
        (418, "provider_validation_response_envelope"),
        (429, "provider_rate_limited"),
        (503, "provider_unavailable"),
    ],
)
def test_account_snapshot_translates_only_allowlisted_provider_errors(
    status_code: int, expected: str
) -> None:
    async def exercise() -> None:
        client = HttpSchwabReadClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(status_code, text="private response"))
        )
        try:
            with pytest.raises(SchwabOAuthError, match=expected) as captured:
                await client.account_snapshot(
                    access_token=token_grant().access_token,
                    provider_account_hash="synthetic-account-hash",
                )
            assert "private response" not in str(captured.value)
        finally:
            await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, "provider_validation_response_envelope"),
        (
            {"securitiesAccount": {"accountNumber": "synthetic-account-hash"}},
            "provider_validation_account_identity_mechanics",
        ),
        (
            {
                "securitiesAccount": {
                    "accountNumber": "999900001111",
                    "type": "CASH",
                    "currentBalances": {
                        "totalCash": "bad",
                        "longNonMarginableMarketValue": 0,
                    },
                    "positions": [],
                }
            },
            "provider_validation_numeric_value",
        ),
        (
            {
                "securitiesAccount": {
                    "accountNumber": "999900001111",
                    "type": "CASH",
                    "currentBalances": {
                        "totalCash": 0,
                        "longNonMarginableMarketValue": 1,
                    },
                    "positions": [
                        {
                            "longQuantity": 1,
                            "shortQuantity": 0,
                            "averagePrice": 1,
                            "marketValue": 1,
                            "instrument": {
                                "assetType": "OPTION",
                                "cusip": "synthetic-key",
                                "symbol": "SYNX",
                            },
                        }
                    ],
                }
            },
            "provider_validation_instrument_type_unsupported",
        ),
    ],
)
def test_account_snapshot_rejects_partial_or_invalid_provider_data(payload: object, expected: str) -> None:
    async def exercise() -> None:
        client = HttpSchwabReadClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"content-type": "application/json"}, json=payload)
            )
        )
        try:
            with pytest.raises(SchwabOAuthError, match=expected):
                await client.account_snapshot(
                    access_token=token_grant().access_token,
                    provider_account_hash="synthetic-account-hash",
                )
        finally:
            await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("missing_field", "expected"),
    [
        ("cusip", "provider_validation_instrument_identity"),
        ("symbol", "provider_validation_instrument_identity"),
        ("longQuantity", "provider_validation_numeric_value"),
        ("marketValue", "provider_validation_numeric_value"),
        ("averagePrice", "provider_validation_numeric_value"),
    ],
)
def test_account_snapshot_requires_identity_and_valuation_fields(missing_field: str, expected: str) -> None:
    payload = provider_account_snapshot_payload()
    account = payload["securitiesAccount"]
    assert isinstance(account, dict)
    positions = account["positions"]
    assert isinstance(positions, list)
    position = positions[0]
    target = position["instrument"] if missing_field in {"cusip", "symbol"} else position
    target.pop(missing_field)

    async def exercise() -> None:
        client = HttpSchwabReadClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"content-type": "application/json"}, json=payload)
            )
        )
        try:
            with pytest.raises(SchwabOAuthError, match=expected):
                await client.account_snapshot(
                    access_token=token_grant().access_token,
                    provider_account_hash="synthetic-account-hash",
                )
        finally:
            await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("instrument_structure", "provider_validation_instrument_structure"),
        ("instrument_metadata", "provider_validation_instrument_metadata"),
        ("instrument_type", "provider_validation_instrument_type_unsupported"),
        ("instrument_type_missing", "provider_validation_instrument_type_malformed"),
        ("instrument_type_null", "provider_validation_instrument_type_malformed"),
        ("instrument_type_non_text", "provider_validation_instrument_type_malformed"),
        ("instrument_type_empty", "provider_validation_instrument_type_malformed"),
        ("instrument_type_whitespace", "provider_validation_instrument_type_malformed"),
        ("instrument_type_bad_format", "provider_validation_instrument_type_malformed"),
    ],
)
def test_account_snapshot_categorizes_instrument_validation_without_private_detail(
    variant: str, expected: str
) -> None:
    payload = provider_account_snapshot_payload()
    account = payload["securitiesAccount"]
    assert isinstance(account, dict)
    positions = account["positions"]
    assert isinstance(positions, list)
    position = positions[0]
    instrument = position["instrument"]
    assert isinstance(instrument, dict)
    if variant == "instrument_structure":
        position["instrument"] = "synthetic-invalid-structure"
    elif variant == "instrument_metadata":
        instrument["description"] = ""
    elif variant == "instrument_type_missing":
        instrument.pop("assetType")
    elif variant == "instrument_type_null":
        instrument["assetType"] = None
    elif variant == "instrument_type_non_text":
        instrument["assetType"] = 7
    elif variant == "instrument_type_empty":
        instrument["assetType"] = ""
    elif variant == "instrument_type_whitespace":
        instrument["assetType"] = " "
    elif variant == "instrument_type_bad_format":
        instrument["assetType"] = "collective investment"
    else:
        instrument["assetType"] = "SYNTHETIC_UNSUPPORTED"

    async def exercise() -> None:
        client = HttpSchwabReadClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"content-type": "application/json"}, json=payload)
            )
        )
        try:
            with pytest.raises(SchwabOAuthError, match=expected) as captured:
                await client.account_snapshot(
                    access_token=token_grant().access_token,
                    provider_account_hash="synthetic-account-hash",
                )
            rendered = str(captured.value)
            assert rendered == expected
            assert all(
                private_value not in rendered
                for private_value in (
                    "synthetic-provider-instrument-key",
                    "SYNX",
                    "Synthetic ETF",
                    "SYNTHETIC_UNSUPPORTED",
                )
            )
        finally:
            await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("missing", "provider_validation_instrument_subtype_malformed"),
        ("null", "provider_validation_instrument_subtype_malformed"),
        ("non_text", "provider_validation_instrument_subtype_malformed"),
        ("empty", "provider_validation_instrument_subtype_malformed"),
        ("whitespace", "provider_validation_instrument_subtype_malformed"),
        ("bad_format", "provider_validation_instrument_subtype_malformed"),
    ],
)
def test_collective_investment_requires_supported_subtype_without_echo(variant: str, expected: str) -> None:
    payload = provider_account_snapshot_payload()
    account = payload["securitiesAccount"]
    assert isinstance(account, dict)
    positions = account["positions"]
    assert isinstance(positions, list)
    instrument = positions[0]["instrument"]
    assert isinstance(instrument, dict)
    instrument["assetType"] = "COLLECTIVE_INVESTMENT"
    if variant == "null":
        instrument["type"] = None
    elif variant == "non_text":
        instrument["type"] = 7
    elif variant == "empty":
        instrument["type"] = ""
    elif variant == "whitespace":
        instrument["type"] = " "
    elif variant == "bad_format":
        instrument["type"] = "exchange traded fund"

    async def exercise() -> None:
        client = HttpSchwabReadClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"content-type": "application/json"}, json=payload)
            )
        )
        try:
            with pytest.raises(SchwabOAuthError, match=expected) as captured:
                await client.account_snapshot(
                    access_token=token_grant().access_token,
                    provider_account_hash="synthetic-account-hash",
                )
            assert str(captured.value) == expected
        finally:
            await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "subtype",
    ["UNIT_INVESTMENT_TRUST", "CLOSED_END_FUND", "INDEX", "UNITS"],
)
def test_collective_investment_rejects_unsupported_subtype_without_echo(
    subtype: str,
) -> None:
    payload = provider_account_snapshot_payload()
    account = payload["securitiesAccount"]
    assert isinstance(account, dict)
    positions = account["positions"]
    assert isinstance(positions, list)
    instrument = positions[0]["instrument"]
    assert isinstance(instrument, dict)
    instrument["assetType"] = "COLLECTIVE_INVESTMENT"
    instrument["type"] = subtype

    async def exercise() -> None:
        client = HttpSchwabReadClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"content-type": "application/json"}, json=payload)
            )
        )
        try:
            expected = "provider_validation_instrument_subtype_unsupported"
            with pytest.raises(SchwabOAuthError, match=expected) as captured:
                await client.account_snapshot(
                    access_token=token_grant().access_token,
                    provider_account_hash="synthetic-account-hash",
                )
            assert str(captured.value) == expected
            assert subtype not in str(captured.value)
        finally:
            await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "asset_type",
    ["OPTION", "FUTURE", "FOREX", "INDEX", "PRODUCT", "CURRENCY", "SYNTHETIC_UNKNOWN"],
)
def test_account_snapshot_rejects_risky_or_unknown_instrument_types_without_echo(
    asset_type: str,
) -> None:
    payload = provider_account_snapshot_payload()
    account = payload["securitiesAccount"]
    assert isinstance(account, dict)
    positions = account["positions"]
    assert isinstance(positions, list)
    positions[0]["instrument"]["assetType"] = asset_type

    async def exercise() -> None:
        client = HttpSchwabReadClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"content-type": "application/json"}, json=payload)
            )
        )
        try:
            with pytest.raises(
                SchwabOAuthError, match="provider_validation_instrument_type_unsupported"
            ) as captured:
                await client.account_snapshot(
                    access_token=token_grant().access_token,
                    provider_account_hash="synthetic-account-hash",
                )
            assert str(captured.value) == "provider_validation_instrument_type_unsupported"
            assert asset_type not in str(captured.value)
        finally:
            await client.close()

    asyncio.run(exercise())


def test_account_snapshot_rejects_duplicate_provider_instruments() -> None:
    payload = provider_account_snapshot_payload()
    account = payload["securitiesAccount"]
    assert isinstance(account, dict)
    positions = account["positions"]
    assert isinstance(positions, list)
    positions.append(dict(positions[0]))

    async def exercise() -> None:
        client = HttpSchwabReadClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"content-type": "application/json"}, json=payload)
            )
        )
        try:
            with pytest.raises(SchwabOAuthError, match="provider_validation_position_structure"):
                await client.account_snapshot(
                    access_token=token_grant().access_token,
                    provider_account_hash="synthetic-account-hash",
                )
        finally:
            await client.close()

    asyncio.run(exercise())


def test_account_snapshot_rejects_unsupported_or_borrowed_margin_balances() -> None:
    unsupported = provider_account_snapshot_payload("CASH")
    unsupported_account = unsupported["securitiesAccount"]
    assert isinstance(unsupported_account, dict)
    unsupported_account["type"] = "IRA"
    borrowed = provider_account_snapshot_payload("MARGIN")
    borrowed_account = borrowed["securitiesAccount"]
    assert isinstance(borrowed_account, dict)
    borrowed_account["currentBalances"] = {"equity": 75, "longMarginValue": 100}
    short = provider_account_snapshot_payload("CASH")
    short_account = short["securitiesAccount"]
    assert isinstance(short_account, dict)
    short_positions = short_account["positions"]
    assert isinstance(short_positions, list)
    short_positions[0]["shortQuantity"] = 1

    async def exercise(payload: object, expected: str) -> None:
        client = HttpSchwabReadClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"content-type": "application/json"}, json=payload)
            )
        )
        try:
            with pytest.raises(SchwabOAuthError, match=expected):
                await client.account_snapshot(
                    access_token=token_grant().access_token,
                    provider_account_hash="synthetic-account-hash",
                )
        finally:
            await client.close()

    asyncio.run(exercise(unsupported, "provider_validation_account_identity_mechanics"))
    asyncio.run(exercise(borrowed, "provider_validation_unsupported_borrowing_short_position"))
    asyncio.run(exercise(short, "provider_validation_unsupported_borrowing_short_position"))


def test_account_snapshot_rejects_wrong_content_type_oversize_timeout_and_cancellation() -> None:
    class OversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * (MAX_SNAPSHOT_RESPONSE_BYTES + 1)

        async def aclose(self) -> None:
            return None

    async def exercise() -> None:
        access_token = token_grant().access_token
        wrong_type = HttpSchwabReadClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text="{}"))
        )
        oversized = HttpSchwabReadClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "application/json"}, stream=OversizedStream()
                )
            )
        )
        timed_out = HttpSchwabReadClient(
            transport=httpx.MockTransport(lambda _: (_ for _ in ()).throw(httpx.ReadTimeout("synthetic")))
        )
        try:
            for client, code in (
                (wrong_type, "provider_validation_response_envelope"),
                (oversized, "provider_response_too_large"),
                (timed_out, "provider_timeout"),
            ):
                with pytest.raises(SchwabOAuthError, match=code):
                    await client.account_snapshot(
                        access_token=access_token, provider_account_hash="synthetic-account-hash"
                    )

            cancelled_client = HttpSchwabReadClient(
                transport=httpx.MockTransport(lambda _: (_ for _ in ()).throw(asyncio.CancelledError()))
            )
            try:
                cancelled = asyncio.create_task(
                    cancelled_client.account_snapshot(
                        access_token=access_token, provider_account_hash="synthetic-account-hash"
                    )
                )
                with pytest.raises(asyncio.CancelledError):
                    await cancelled
            finally:
                await cancelled_client.close()
        finally:
            await wrong_type.close()
            await oversized.close()
            await timed_out.close()

    asyncio.run(exercise())


def test_bulk_mapping_rejects_conflicts_without_partial_commit(settings: Settings) -> None:
    _, portfolio, connections, connection_id = connected_repository(settings)
    first = portfolio.create_account(
        name="Synthetic first", account_type="roth_ira", tax_treatment="tax_free"
    )
    second = portfolio.create_account(
        name="Synthetic second", account_type="hsa", tax_treatment="tax_advantaged"
    )
    with pytest.raises(ValueError, match="duplicated"):
        connections.map_accounts(
            connection_id,
            (
                ("synthetic-provider-hash-one", first.id, "1111"),
                ("synthetic-provider-hash-two", first.id, "2222"),
            ),
        )
    saved = connections.map_accounts(
        connection_id,
        (
            ("synthetic-provider-hash-one", first.id, "1111"),
            ("synthetic-provider-hash-two", second.id, "2222"),
        ),
    )
    repeated = connections.map_accounts(
        connection_id,
        (
            ("synthetic-provider-hash-one", first.id, "1111"),
            ("synthetic-provider-hash-two", second.id, "2222"),
        ),
    )
    assert [item.id for item in repeated] == [item.id for item in saved]

    with pytest.raises(ValueError, match="identity changed"):
        connections.map_accounts(
            connection_id,
            (("synthetic-provider-hash-one", first.id, "9999"),),
        )

    cash = portfolio.create_account(
        name="Synthetic cash", account_type="cash", tax_treatment="not_applicable"
    )
    with pytest.raises(ValueError, match="unavailable"):
        connections.map_accounts(
            connection_id,
            (("synthetic-provider-hash-three", cash.id, "3333"),),
        )


def test_mapping_selection_persists_across_repository_restart_and_encrypted_restore(
    settings: Settings, tmp_path: Path
) -> None:
    database, portfolio, connections, connection_id = connected_repository(settings)
    account = portfolio.create_account(
        name="Synthetic mapped account", account_type="roth_ira", tax_treatment="tax_free"
    )
    provider_hash = "synthetic-provider-hash-persisted"
    connections.map_accounts(connection_id, ((provider_hash, account.id, "1111"),))
    assert connections.mapped_account_ids(connection_id, ((provider_hash, "1111"),)) == (account.id,)

    restarted = ProviderConnectionRepository(
        Database(settings.database_path, settings.data_vault(), settings.database_key)
    )
    assert restarted.mapped_account_ids(connection_id, ((provider_hash, "1111"),)) == (account.id,)

    service = BackupService(database, settings.backup_dir, settings.backup_vault())
    backup = service.create()
    restored_database = database.at_path(tmp_path / "restored" / "northstar.db")
    service.restore_to(backup, restored_database.path)
    restored = ProviderConnectionRepository(restored_database)
    assert restored.mapped_account_ids(connection_id, ((provider_hash, "1111"),)) == (account.id,)
    with pytest.raises(ValueError, match="identity changed"):
        restored.mapped_account_ids(connection_id, ((provider_hash, "9999"),))


def test_discovery_preview_is_redacted_retried_and_replaced() -> None:
    reference = SchwabAccountReference("synthetic-account-hash", "1111")
    service = SchwabDiscoveryService()
    calls = 0

    async def load_accounts() -> tuple[SchwabAccountReference, ...]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SchwabOAuthError("provider_rate_limited")
        return (reference,)

    async def exercise() -> None:
        first = await service.discover(connection_id="synthetic-connection", load_accounts=load_accounts)
        second = await service.discover(connection_id="synthetic-connection", load_accounts=load_accounts)
        assert reference.provider_account_hash not in repr(reference)
        assert first.token not in repr(first)
        assert reference.provider_account_hash not in repr(first)
        with pytest.raises(DiscoveryError, match="preview_invalid"):
            service.consume(token=first.token, connection_id="synthetic-connection")
        consumed = service.consume(token=second.token, connection_id="synthetic-connection")
        assert consumed.accounts == (reference,)
        with pytest.raises(DiscoveryError, match="preview_invalid"):
            service.consume(token=second.token, connection_id="synthetic-connection")

    asyncio.run(exercise())
    assert calls == 3


def test_replacement_discovery_invalidates_the_previous_preview_before_provider_returns() -> None:
    service = SchwabDiscoveryService()
    reference = SchwabAccountReference("synthetic-account-hash", "1111")
    started = asyncio.Event()
    release = asyncio.Event()

    async def initial_loader() -> tuple[SchwabAccountReference, ...]:
        return (reference,)

    async def replacement_loader() -> tuple[SchwabAccountReference, ...]:
        started.set()
        await release.wait()
        return (reference,)

    async def exercise() -> None:
        initial = await service.discover(connection_id="synthetic-connection", load_accounts=initial_loader)
        replacement = asyncio.create_task(
            service.discover(connection_id="synthetic-connection", load_accounts=replacement_loader)
        )
        await started.wait()
        with pytest.raises(DiscoveryError, match="preview_invalid"):
            service.consume(token=initial.token, connection_id="synthetic-connection")
        release.set()
        await replacement

    asyncio.run(exercise())
