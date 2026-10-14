from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt import PyJWK
from jwt.exceptions import PyJWKClientConnectionError

from northstar.auth.access import AuthenticatedUser, CloudflareAccessVerifier
from northstar.config import Settings
from northstar.main import create_app

ISSUER = "https://synthetic-team.cloudflareaccess.com"
AUDIENCE = "synthetic-application-audience"
ALLOWED_EMAIL = "synthetic-user@northstar.invalid"
PUBLIC_HOST = "northstar.example.com"
SYNTHETIC_ASSERTION = "synthetic-valid-access-assertion"


class StaticJwks:
    def __init__(self, keys: dict[str, PyJWK], *, unavailable: bool = False) -> None:
        self.keys = keys
        self.unavailable = unavailable
        self.requested: list[str] = []

    def get_signing_key_from_jwt(self, token: str) -> PyJWK:
        if self.unavailable:
            raise PyJWKClientConnectionError("synthetic JWK outage")
        kid = jwt.get_unverified_header(token).get("kid")
        if not isinstance(kid, str) or kid not in self.keys:
            raise jwt.PyJWKClientError("synthetic key is unavailable")
        self.requested.append(kid)
        return self.keys[kid]


class AcceptingVerifier:
    async def verify(self, token: str) -> AuthenticatedUser:
        if token != SYNTHETIC_ASSERTION:
            raise jwt.InvalidTokenError("synthetic rejection")
        return AuthenticatedUser(ALLOWED_EMAIL, "synthetic-subject")


class UnavailableVerifier:
    async def verify(self, _: str) -> AuthenticatedUser:
        raise PyJWKClientConnectionError("synthetic JWK outage")


def _key(kid: str) -> tuple[rsa.RSAPrivateKey, PyJWK]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    payload = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    payload.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return private, PyJWK.from_dict(payload)


def _claims(**changes: Any) -> dict[str, Any]:
    now = int(time.time())
    values: dict[str, Any] = {
        "iss": ISSUER,
        "aud": [AUDIENCE],
        "sub": "synthetic-subject",
        "email": ALLOWED_EMAIL,
        "iat": now - 1,
        "nbf": now - 1,
        "exp": now + 300,
    }
    values.update(changes)
    return values


def _token(
    private: rsa.RSAPrivateKey,
    kid: str,
    *,
    claims: dict[str, Any] | None = None,
) -> str:
    return jwt.encode(claims or _claims(), private, algorithm="RS256", headers={"kid": kid})


def _verifier(key: PyJWK, *, unavailable: bool = False) -> CloudflareAccessVerifier:
    return CloudflareAccessVerifier(
        ISSUER,
        AUDIENCE,
        f"  {ALLOWED_EMAIL.upper()}  ",
        jwks=StaticJwks({"key-one": key}, unavailable=unavailable),
    )


def _production_settings(settings: Settings, tmp_path: Path) -> Settings:
    runtime = tmp_path / "runtime"
    backups = runtime / "backups"
    runtime.mkdir(mode=0o700)
    backups.mkdir(mode=0o700)
    secrets = runtime / "secrets.env"
    secrets.write_text("synthetic placeholders only\n", encoding="utf-8")
    secrets.chmod(0o600)
    production = replace(
        settings,
        environment="production",
        auth_mode="cloudflare",
        database_path=runtime / "northstar.db",
        backup_dir=backups,
        secrets_path=secrets,
        cf_issuer=ISSUER,
        cf_audience=AUDIENCE,
        allowed_email=ALLOWED_EMAIL,
        secure_cookies=True,
    )
    production.validate()
    return production


def test_rs256_access_assertion_requires_exact_identity_and_claims() -> None:
    private, public = _key("key-one")
    user = asyncio.run(_verifier(public).verify(_token(private, "key-one")))

    assert user == AuthenticatedUser(ALLOWED_EMAIL, "synthetic-subject")


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://other-team.cloudflareaccess.com"},
        {"aud": ["wrong-application-audience"]},
        {"aud": [AUDIENCE, "unexpected-second-audience"]},
        {"email": "other-user@northstar.invalid"},
        {"exp": int(time.time()) - 1},
        {"iat": int(time.time()) + 120},
        {"nbf": int(time.time()) + 120},
        {"sub": ""},
        {"sub": " synthetic-subject"},
        {"sub": "synthetic\nsubject"},
        {"sub": "s" * 513},
    ],
)
def test_access_assertion_rejects_wrong_or_inactive_claims(changes: dict[str, Any]) -> None:
    private, public = _key("key-one")

    with pytest.raises(jwt.PyJWTError):
        asyncio.run(_verifier(public).verify(_token(private, "key-one", claims=_claims(**changes))))


@pytest.mark.parametrize("missing", ["iss", "aud", "sub", "email", "iat", "nbf", "exp"])
def test_access_assertion_rejects_every_missing_required_claim(missing: str) -> None:
    private, public = _key("key-one")
    claims = _claims()
    del claims[missing]

    with pytest.raises(jwt.MissingRequiredClaimError):
        asyncio.run(_verifier(public).verify(_token(private, "key-one", claims=claims)))


def test_access_assertion_rejects_wrong_algorithm_and_signature() -> None:
    private, public = _key("key-one")
    other_private, _ = _key("other-key")
    wrong_algorithm = jwt.encode(
        _claims(),
        "synthetic-hmac-key-that-is-not-used-in-production",
        algorithm="HS256",
        headers={"kid": "key-one"},
    )
    wrong_signature = _token(other_private, "key-one")

    with pytest.raises(jwt.InvalidAlgorithmError):
        asyncio.run(_verifier(public).verify(wrong_algorithm))
    with pytest.raises(jwt.InvalidSignatureError):
        asyncio.run(_verifier(public).verify(wrong_signature))


def test_jwk_rotation_uses_new_key_id_and_network_failure_denies() -> None:
    first_private, first_public = _key("key-one")
    second_private, second_public = _key("key-two")
    provider = StaticJwks({"key-one": first_public, "key-two": second_public})
    verifier = CloudflareAccessVerifier(ISSUER, AUDIENCE, ALLOWED_EMAIL, jwks=provider)

    asyncio.run(verifier.verify(_token(first_private, "key-one")))
    asyncio.run(verifier.verify(_token(second_private, "key-two")))

    assert provider.requested == ["key-one", "key-two"]
    with pytest.raises(PyJWKClientConnectionError):
        asyncio.run(_verifier(first_public, unavailable=True).verify(_token(first_private, "key-one")))


def test_jwk_client_has_fixed_endpoint_bounded_set_cache_and_timeout() -> None:
    verifier = CloudflareAccessVerifier(ISSUER, AUDIENCE, ALLOWED_EMAIL)

    assert verifier.jwks.uri == f"{ISSUER}/cdn-cgi/access/certs"  # type: ignore[attr-defined]
    assert verifier.jwks.timeout == 5  # type: ignore[attr-defined]
    assert verifier.jwks.jwk_set_cache.lifespan == 300  # type: ignore[attr-defined]
    assert not hasattr(verifier.jwks.get_signing_key, "cache_parameters")


@pytest.mark.parametrize(
    "issuer",
    [
        "http://synthetic-team.cloudflareaccess.com",
        "https://synthetic-team.cloudflareaccess.com/",
        "https://synthetic-team.cloudflareaccess.com/path",
        "https://synthetic-team.cloudflareaccess.com?query=value",
        "https://synthetic-team.cloudflareaccess.com:443",
        "https://extra.synthetic-team.cloudflareaccess.com",
        "https://synthetic-team.cloudflareaccess.com.attacker.invalid",
        "https://user@synthetic-team.cloudflareaccess.com",
    ],
)
def test_cloudflare_issuer_allows_only_exact_fixed_team_origin(issuer: str) -> None:
    with pytest.raises(ValueError, match="exact team issuer"):
        CloudflareAccessVerifier(issuer, AUDIENCE, ALLOWED_EMAIL)


def test_production_origin_boundary_and_public_health_require_access(
    settings: Settings, tmp_path: Path
) -> None:
    production = _production_settings(settings, tmp_path)
    app = create_app(production, access_verifier=AcceptingVerifier())
    valid = {"Host": PUBLIC_HOST, "Cf-Access-Jwt-Assertion": SYNTHETIC_ASSERTION}

    with TestClient(app) as client:
        local_health = client.get("/healthz", headers={"Host": "127.0.0.1:8000"})
        public_health_denied = client.get("/healthz", headers={"Host": PUBLIC_HOST})
        public_health = client.get("/healthz", headers=valid)
        direct_forgery = client.get(
            "/", headers={"Host": "127.0.0.1:8010", "Cf-Access-Jwt-Assertion": "forged"}
        )
        invalid_host = client.get("/", headers=valid | {"Host": "attacker.invalid"})
        invalid_forwarded_host = client.get("/", headers=valid | {"X-Forwarded-Host": "attacker.invalid"})
        invalid_forwarded_scheme = client.get("/", headers=valid | {"X-Forwarded-Proto": "http"})
        accepted = client.get(
            "/", headers=valid | {"X-Forwarded-Host": PUBLIC_HOST, "X-Forwarded-Proto": "https"}
        )

    assert local_health.status_code == 200
    assert local_health.json() == {"status": "ok"}
    assert public_health_denied.status_code == 401
    assert public_health.status_code == 200
    assert direct_forgery.status_code == 401
    assert invalid_host.status_code == 400
    assert invalid_forwarded_host.status_code == 400
    assert invalid_forwarded_scheme.status_code == 400
    assert accepted.status_code == 200
    assert accepted.headers["Cache-Control"] == "no-store, private, max-age=0"
    assert accepted.headers["Strict-Transport-Security"] == "max-age=86400"
    assert "includesubdomains" not in accepted.headers["Strict-Transport-Security"].lower()
    assert production.public_origin == "https://northstar.example.com"


def test_production_session_cookie_is_host_only_secure_http_only_and_lax(
    settings: Settings, tmp_path: Path
) -> None:
    production = _production_settings(settings, tmp_path)
    app = create_app(production, access_verifier=AcceptingVerifier())

    with TestClient(app) as client:
        response = client.get(
            "/",
            headers={
                "Host": PUBLIC_HOST,
                "Cf-Access-Jwt-Assertion": SYNTHETIC_ASSERTION,
            },
        )

    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-northstar_session=")
    assert "path=/" in cookie.lower()
    assert "secure" in cookie.lower()
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert "domain=" not in cookie.lower()


def test_access_failure_and_logs_never_echo_assertion_identity_or_claims(
    settings: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    production = _production_settings(settings, tmp_path)
    assertion = "synthetic-private-assertion-that-must-not-be-logged"
    caplog.set_level(logging.DEBUG)

    with TestClient(create_app(production, access_verifier=UnavailableVerifier())) as client:
        response = client.get(
            "/",
            headers={"Host": PUBLIC_HOST, "Cf-Access-Jwt-Assertion": assertion},
        )

    rendered = response.text + caplog.text
    assert response.status_code == 401
    assert response.json() == {"error": "Authentication required"}
    assert assertion not in rendered
    assert ALLOWED_EMAIL not in rendered
    assert "synthetic-subject" not in rendered
