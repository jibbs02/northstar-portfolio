from __future__ import annotations

import logging
import re
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html import unescape
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi.testclient import TestClient

from northstar.config import Settings
from northstar.integrations import SchwabAccountReference, SchwabOAuthError, SchwabTokenGrant
from northstar.integrations.schwab import SCHWAB_AUTHORIZE_URL
from northstar.main import create_app

_DEFAULT_CSP = (
    "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'; object-src 'none'; img-src 'self' data:; "
    "style-src 'self'; script-src 'self'; connect-src 'self'"
)
_SYNTHETIC_CLIENT_ID = "synthetic-placeholder-client-id"
_SYNTHETIC_REDIRECT_URI = "http://127.0.0.1:8010/integrations/schwab/callback"


def _csrf(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match is not None
    return match.group(1)


def _authorization_url(page: str) -> str:
    match = re.search(
        r'<a class="button-link" href="([^"]+)" rel="noreferrer">Continue to Schwab</a>',
        page,
    )
    assert match is not None
    return unescape(match.group(1))


def _visible_text(page: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", page))


def _enabled(settings: Settings) -> Settings:
    synthetic_secret = "-".join(("synthetic", "placeholder", "client", "secret"))
    enabled = replace(
        settings,
        schwab_enabled=True,
        schwab_client_id=_SYNTHETIC_CLIENT_ID,
        schwab_client_secret=synthetic_secret,
        schwab_redirect_uri=_SYNTHETIC_REDIRECT_URI,
    )
    enabled.validate()
    return enabled


def _grant() -> SchwabTokenGrant:
    now = datetime.now(UTC)
    synthetic_access = "-".join(("synthetic", "placeholder", "access", "credential"))
    synthetic_refresh = "-".join(("synthetic", "placeholder", "refresh", "credential"))
    bearer_type = "Bearer"
    return SchwabTokenGrant(
        access_token=synthetic_access,
        refresh_token=synthetic_refresh,
        token_type=bearer_type,
        expires_at=now + timedelta(minutes=30),
        refresh_expires_at=now + timedelta(days=7),
        scopes=("api",),
    )


class FakeSchwabOAuthClient:
    def __init__(self) -> None:
        self.states: list[str] = []
        self.codes: list[str] = []
        self.before_exchange: Callable[[], None] | None = None
        self.exchange_error: SchwabOAuthError | None = None

    def authorization_url(self, *, state: str) -> str:
        self.states.append(state)
        query = urlencode(
            {
                "client_id": _SYNTHETIC_CLIENT_ID,
                "redirect_uri": _SYNTHETIC_REDIRECT_URI,
                "state": state,
            }
        )
        return f"{SCHWAB_AUTHORIZE_URL}?{query}"

    async def exchange_authorization_code(self, *, code: str) -> SchwabTokenGrant:
        self.codes.append(code)
        if self.before_exchange is not None:
            self.before_exchange()
        if self.exchange_error is not None:
            raise self.exchange_error
        return _grant()

    async def refresh_access(self, *, refresh_token: str) -> SchwabTokenGrant:
        del refresh_token
        return _grant()


class FakeSchwabReadClient:
    def __init__(self) -> None:
        self.references = (SchwabAccountReference("synthetic-provider-hash", "1111"),)
        self.calls = 0

    async def linked_accounts(self, *, access_token: str) -> tuple[SchwabAccountReference, ...]:
        del access_token
        self.calls += 1
        return self.references


def _start(client: TestClient) -> tuple[str, str]:
    settings_page = client.get("/settings")
    assert settings_page.headers["Content-Security-Policy"] == _DEFAULT_CSP
    csrf = _csrf(settings_page.text)
    response = client.post(
        "/integrations/schwab/connect",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert response.headers["Content-Security-Policy"] == _DEFAULT_CSP
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store, private, max-age=0"
    assert "location" not in response.headers
    parsed = urlsplit(_authorization_url(response.text))
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == SCHWAB_AUTHORIZE_URL
    state = parse_qs(parsed.query)["state"][0]
    return csrf, state


def test_confirmation_link_ends_the_form_navigation_without_exposing_oauth_values(
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    oauth = FakeSchwabOAuthClient()
    app = create_app(_enabled(settings), schwab_oauth_client=oauth)
    with TestClient(app) as client:
        page = client.get("/settings")
        caplog.set_level(logging.DEBUG)
        response = client.post(
            "/integrations/schwab/connect",
            data={"csrf_token": _csrf(page.text)},
            follow_redirects=False,
        )
        ordinary_redirect = client.post(
            "/demo/verify-storage",
            data={"csrf_token": _csrf(client.get("/").text)},
            follow_redirects=False,
        )
        ordinary_page = client.get("/")

    authorization_url = _authorization_url(response.text)
    parsed = urlsplit(authorization_url)
    visible_text = _visible_text(response.text)
    log_messages = " ".join(record.getMessage() for record in caplog.records)
    assert response.status_code == 200
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == SCHWAB_AUTHORIZE_URL
    assert parse_qs(parsed.query)["state"] == oauth.states
    assert response.headers["Cache-Control"] == "no-store, private, max-age=0"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert 'href="/settings">Cancel and return to Settings</a>' in response.text
    assert authorization_url not in visible_text
    assert oauth.states[0] not in visible_text
    assert _SYNTHETIC_CLIENT_ID not in visible_text
    assert authorization_url not in log_messages
    assert oauth.states[0] not in log_messages
    assert _SYNTHETIC_CLIENT_ID not in log_messages
    assert page.headers["Content-Security-Policy"] == _DEFAULT_CSP
    assert response.headers["Content-Security-Policy"] == _DEFAULT_CSP
    assert ordinary_redirect.headers["Content-Security-Policy"] == _DEFAULT_CSP
    assert ordinary_page.headers["Content-Security-Policy"] == _DEFAULT_CSP


def test_disabled_schwab_routes_fail_closed(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        page = client.get("/settings")
        denied = client.post(
            "/integrations/schwab/connect",
            data={"csrf_token": _csrf(page.text)},
            follow_redirects=False,
        )
        callback = client.get(
            "/integrations/schwab/callback?state=synthetic&code=synthetic",
            follow_redirects=False,
        )

    assert "Schwab is disabled" in page.text
    assert page.headers["Content-Security-Policy"] == _DEFAULT_CSP
    assert denied.status_code == 404
    assert callback.status_code == 404


def test_discovery_preview_is_no_store_filtered_and_one_time(settings: Settings) -> None:
    oauth = FakeSchwabOAuthClient()
    read = FakeSchwabReadClient()
    app = create_app(_enabled(settings), schwab_oauth_client=oauth, schwab_read_client=read)
    with TestClient(app) as client:
        _, state = _start(client)
        callback = client.get(
            "/integrations/schwab/callback",
            params={"state": state, "code": "synthetic-code"},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        roth = app.state.portfolio.create_account(
            name="Synthetic Roth", account_type="roth_ira", tax_treatment="tax_free"
        )
        app.state.portfolio.create_account(
            name="Synthetic cash", account_type="cash", tax_treatment="not_applicable"
        )
        preview = client.post(
            "/integrations/schwab/discovery",
            data={"csrf_token": _csrf(client.get("/settings").text)},
            follow_redirects=False,
        )
        assert preview.status_code == 200
        assert "no-store" in preview.headers["Cache-Control"]
        assert "••••1111" in preview.text
        assert "Synthetic Roth" in preview.text
        assert "Synthetic cash" not in preview.text
        assert "synthetic-provider-hash" not in preview.text
        preview_token = re.search(r'name="preview_token" value="([^"]+)"', preview.text)
        assert preview_token is not None
        confirm = client.post(
            "/integrations/schwab/discovery/confirm",
            data={
                "csrf_token": _csrf(preview.text),
                "preview_token": preview_token.group(1),
                "mapping_0": roth.id,
            },
            follow_redirects=False,
        )
        rediscovery = client.post(
            "/integrations/schwab/discovery",
            data={"csrf_token": _csrf(client.get("/settings").text)},
            follow_redirects=False,
        )
        replay = client.post(
            "/integrations/schwab/discovery/confirm",
            data={
                "csrf_token": _csrf(client.get("/settings").text),
                "preview_token": preview_token.group(1),
                "mapping_0": roth.id,
            },
            follow_redirects=False,
        )
        read.references = (SchwabAccountReference("synthetic-provider-hash", "9999"),)
        suffix_conflict = client.post(
            "/integrations/schwab/discovery",
            data={"csrf_token": _csrf(client.get("/settings").text)},
            follow_redirects=False,
        )

    assert confirm.headers["location"] == "/settings?result=schwab_mapping_saved"
    assert f'<input type="hidden" name="mapping_0" value="{roth.id}">' in rediscovery.text
    assert "Existing mappings are fixed here" in rediscovery.text
    assert replay.headers["location"] == "/settings?result=schwab_mapping_failed"
    assert suffix_conflict.headers["location"] == "/settings?result=schwab_discovery_failed"
    assert read.calls == 3


def test_callback_requires_the_normal_application_identity(settings: Settings) -> None:
    protected = replace(
        _enabled(settings),
        auth_mode="cloudflare",
        cf_issuer="https://synthetic.cloudflareaccess.com",
        cf_audience="synthetic-placeholder-audience",
        allowed_email="synthetic@example.com",
    )
    protected.validate()
    app = create_app(protected, schwab_oauth_client=FakeSchwabOAuthClient())

    with TestClient(app) as client:
        response = client.get(
            "/integrations/schwab/callback?state=synthetic&code=synthetic",
            follow_redirects=False,
        )

    assert response.status_code == 401
    assert response.json() == {"error": "Authentication required"}


def test_oauth_state_is_hashed_session_bound_and_single_use(settings: Settings) -> None:
    oauth = FakeSchwabOAuthClient()
    app = create_app(_enabled(settings), schwab_oauth_client=oauth)
    with TestClient(app) as client:
        original_csrf, state = _start(client)
        with closing(app.state.database.connect()) as conn:
            stored = conn.execute("SELECT * FROM oauth_transactions").fetchone()

        callback = client.get(
            "/integrations/schwab/callback",
            params={"state": state, "code": "synthetic-authorization-code"},
            follow_redirects=False,
        )
        replay = client.get(
            "/integrations/schwab/callback",
            params={"state": state, "code": "synthetic-replayed-code"},
            follow_redirects=False,
        )
        rotated_csrf = _csrf(client.get("/settings").text)

    assert stored["state_hash"] != state
    assert stored["session_hash"] != state
    assert stored["encrypted_verifier"] is None
    assert stored["used_at"] is None
    assert callback.status_code == 303
    assert callback.headers["location"] == "/settings?result=schwab_connected"
    assert "synthetic-authorization-code" not in callback.headers["location"]
    assert replay.headers["location"] == "/settings?result=schwab_invalid"
    assert oauth.codes == ["synthetic-authorization-code"]
    assert rotated_csrf != original_csrf
    connection = app.state.provider_connections.for_provider()
    assert connection is not None and connection.status == "connected"
    assert app.state.provider_connections.token_grant(connection.id) is not None


def test_session_mismatch_does_not_consume_the_valid_state(settings: Settings) -> None:
    oauth = FakeSchwabOAuthClient()
    app = create_app(_enabled(settings), schwab_oauth_client=oauth)
    with TestClient(app) as initiator:
        _, state = _start(initiator)
        intruder = TestClient(app)
        mismatch = intruder.get(
            "/integrations/schwab/callback",
            params={"state": state, "code": "synthetic-wrong-session-code"},
            follow_redirects=False,
        )
        intruder.close()
        accepted = initiator.get(
            "/integrations/schwab/callback",
            params={"state": state, "code": "synthetic-right-session-code"},
            follow_redirects=False,
        )

    assert mismatch.headers["location"] == "/settings?result=schwab_invalid"
    assert accepted.headers["location"] == "/settings?result=schwab_connected"
    assert oauth.codes == ["synthetic-right-session-code"]


def test_expired_and_superseded_states_never_reach_token_exchange(settings: Settings) -> None:
    oauth = FakeSchwabOAuthClient()
    app = create_app(_enabled(settings), schwab_oauth_client=oauth)
    with TestClient(app) as client:
        csrf, first_state = _start(client)
        second = client.post(
            "/integrations/schwab/connect",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        second_state = parse_qs(urlsplit(_authorization_url(second.text)).query)["state"][0]
        superseded = client.get(
            "/integrations/schwab/callback",
            params={"state": first_state, "code": "synthetic-superseded-code"},
            follow_redirects=False,
        )
        with app.state.database.transaction() as conn:
            state_hash = app.state.database.vault.blind_index("oauth-state", second_state)
            conn.execute(
                "UPDATE oauth_transactions SET expires_at=? WHERE state_hash=?",
                ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), state_hash),
            )
        expired = client.get(
            "/integrations/schwab/callback",
            params={"state": second_state, "code": "synthetic-expired-code"},
            follow_redirects=False,
        )
        with closing(app.state.database.connect()) as conn:
            expired_row = conn.execute(
                "SELECT used_at FROM oauth_transactions WHERE state_hash=?", (state_hash,)
            ).fetchone()

    assert superseded.headers["location"] == "/settings?result=schwab_invalid"
    assert expired.headers["location"] == "/settings?result=schwab_invalid"
    assert expired_row["used_at"] is not None
    assert oauth.codes == []


def test_callback_rejects_duplicate_parameters_and_redacts_provider_errors(
    settings: Settings,
) -> None:
    oauth = FakeSchwabOAuthClient()
    app = create_app(_enabled(settings), schwab_oauth_client=oauth)
    private_description = "private-provider-description-must-not-escape"
    with TestClient(app) as client:
        _, duplicate_state = _start(client)
        duplicate = client.get(
            "/integrations/schwab/callback",
            params=[
                ("state", duplicate_state),
                ("state", "synthetic-second-state"),
                ("code", "synthetic-code"),
            ],
            follow_redirects=False,
        )
        _, denied_state = _start(client)
        denied = client.get(
            "/integrations/schwab/callback",
            params={
                "state": denied_state,
                "error": "access_denied",
                "error_description": private_description,
            },
            follow_redirects=False,
        )

    rendered = duplicate.text + str(duplicate.headers) + denied.text + str(denied.headers)
    assert duplicate.headers["location"] == "/settings?result=schwab_invalid"
    assert denied.headers["location"] == "/settings?result=schwab_denied"
    assert private_description not in rendered
    assert oauth.codes == []


def test_oversized_callback_is_rejected_without_consuming_valid_state(settings: Settings) -> None:
    oauth = FakeSchwabOAuthClient()
    app = create_app(_enabled(settings), schwab_oauth_client=oauth)
    with TestClient(app) as client:
        _, state = _start(client)
        oversized = client.get(
            "/integrations/schwab/callback",
            params={
                "state": state,
                "code": "synthetic-code",
                "error_description": "x" * (17 * 1024),
            },
            follow_redirects=False,
        )
        accepted = client.get(
            "/integrations/schwab/callback",
            params={"state": state, "code": "synthetic-authorization-code"},
            follow_redirects=False,
        )

    assert oversized.headers["location"] == "/settings?result=schwab_invalid"
    assert accepted.headers["location"] == "/settings?result=schwab_connected"
    assert oauth.codes == ["synthetic-authorization-code"]


def test_connect_and_disconnect_require_csrf_and_remove_local_tokens(settings: Settings) -> None:
    oauth = FakeSchwabOAuthClient()
    app = create_app(_enabled(settings), schwab_oauth_client=oauth)
    with TestClient(app) as client:
        assert client.post("/integrations/schwab/connect").status_code == 403
        _, state = _start(client)
        client.get(
            "/integrations/schwab/callback",
            params={"state": state, "code": "synthetic-authorization-code"},
            follow_redirects=False,
        )
        assert client.post("/integrations/schwab/disconnect").status_code == 403
        connected_page = client.get("/settings")
        csrf = _csrf(connected_page.text)
        disconnected = client.post(
            "/integrations/schwab/disconnect",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        reconnect_page = client.get("/settings")

    connection = app.state.provider_connections.for_provider()
    assert "Disconnect Schwab" in connected_page.text
    assert "synthetic-placeholder-access-credential" not in connected_page.text
    assert "Connect Schwab" in reconnect_page.text
    assert disconnected.headers["location"] == "/settings?result=schwab_disconnected"
    assert connection is not None and connection.status == "disconnected"
    assert app.state.provider_connections.token_grant(connection.id) is None


def test_disconnect_wins_against_an_in_flight_callback(settings: Settings) -> None:
    oauth = FakeSchwabOAuthClient()
    app = create_app(_enabled(settings), schwab_oauth_client=oauth)
    with TestClient(app) as client:
        _, state = _start(client)
        connection = app.state.provider_connections.for_provider()
        assert connection is not None
        oauth.before_exchange = lambda: app.state.provider_connections.disconnect(connection.id)
        callback = client.get(
            "/integrations/schwab/callback",
            params={"state": state, "code": "synthetic-authorization-code"},
            follow_redirects=False,
        )

    final_connection = app.state.provider_connections.for_provider()
    assert callback.headers["location"] == "/settings?result=schwab_disconnected"
    assert final_connection is not None and final_connection.status == "disconnected"
    assert app.state.provider_connections.token_grant(final_connection.id) is None


def test_new_authorization_wins_against_an_in_flight_callback(settings: Settings) -> None:
    oauth = FakeSchwabOAuthClient()
    app = create_app(_enabled(settings), schwab_oauth_client=oauth)
    replacement: dict[str, str] = {}

    def start_replacement() -> None:
        session_binding = "synthetic-replacement-oauth-session-binding"
        _, state = app.state.provider_connections.start_oauth(session_binding)
        replacement["state_hash"] = app.state.database.vault.blind_index("oauth-state", state)

    with TestClient(app) as client:
        _, state = _start(client)
        oauth.before_exchange = start_replacement
        callback = client.get(
            "/integrations/schwab/callback",
            params={"state": state, "code": "synthetic-authorization-code"},
            follow_redirects=False,
        )
        with closing(app.state.database.connect()) as conn:
            replacement_row = conn.execute(
                "SELECT used_at FROM oauth_transactions WHERE state_hash=?",
                (replacement["state_hash"],),
            ).fetchone()

    final_connection = app.state.provider_connections.for_provider()
    assert callback.headers["location"] == "/settings?result=schwab_invalid"
    assert final_connection is not None and final_connection.status == "pending"
    assert app.state.provider_connections.token_grant(final_connection.id) is None
    assert replacement_row is not None and replacement_row["used_at"] is None


def test_provider_exchange_failure_is_generic_and_consumes_state(settings: Settings) -> None:
    oauth = FakeSchwabOAuthClient()
    oauth.exchange_error = SchwabOAuthError("provider_timeout")
    app = create_app(_enabled(settings), schwab_oauth_client=oauth)
    with TestClient(app) as client:
        _, state = _start(client)
        failed = client.get(
            "/integrations/schwab/callback",
            params={"state": state, "code": "synthetic-authorization-code"},
            follow_redirects=False,
        )
        replay = client.get(
            "/integrations/schwab/callback",
            params={"state": state, "code": "synthetic-replayed-code"},
            follow_redirects=False,
        )

    assert failed.headers["location"] == "/settings?result=schwab_failed"
    assert replay.headers["location"] == "/settings?result=schwab_invalid"
    assert oauth.codes == ["synthetic-authorization-code"]
