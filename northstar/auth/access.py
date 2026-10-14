from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from jwt import PyJWKClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from northstar.config import (
    Settings,
    normalize_allowed_email,
    validate_cloudflare_audience,
    validate_cloudflare_issuer,
)

MAX_ACCESS_JWT_BYTES = 16 * 1024


class SigningKeyProvider(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


@dataclass(frozen=True)
class AuthenticatedUser:
    email: str
    subject: str


class CloudflareAccessVerifier:
    def __init__(
        self,
        issuer: str,
        audience: str,
        allowed_email: str,
        *,
        jwks: SigningKeyProvider | None = None,
    ) -> None:
        self.issuer = validate_cloudflare_issuer(issuer)
        self.audience = validate_cloudflare_audience(audience)
        self.allowed_email = normalize_allowed_email(allowed_email)
        self.jwks = jwks or PyJWKClient(
            f"{self.issuer}/cdn-cgi/access/certs",
            cache_keys=False,
            cache_jwk_set=True,
            lifespan=300,
            timeout=5,
        )

    async def verify(self, token: str) -> AuthenticatedUser:
        return await asyncio.to_thread(self._verify_sync, token)

    def _verify_sync(self, token: str) -> AuthenticatedUser:
        if (
            not token
            or len(token.encode("utf-8")) > MAX_ACCESS_JWT_BYTES
            or any(character.isspace() for character in token)
        ):
            raise jwt.InvalidTokenError("Access token is malformed")
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str) or not header["kid"]:
            raise jwt.InvalidAlgorithmError("Access token algorithm is not allowed")
        signing_key = self.jwks.get_signing_key_from_jwt(token)
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=self.issuer,
            options={
                "require": ["exp", "iat", "nbf", "sub", "email", "aud", "iss"],
                "verify_aud": False,
            },
        )
        audience = claims.get("aud")
        audiences = [audience] if isinstance(audience, str) else audience
        if not isinstance(audiences, list) or audiences != [self.audience]:
            raise jwt.InvalidAudienceError("Access token audience is not allowed")
        if any(
            isinstance(claims.get(name), bool) or not isinstance(claims.get(name), int | float)
            for name in ("exp", "iat", "nbf")
        ):
            raise jwt.InvalidTokenError("Access token time claim is malformed")
        raw_email = claims.get("email")
        raw_subject = claims.get("sub")
        if not isinstance(raw_email, str) or not isinstance(raw_subject, str):
            raise jwt.InvalidTokenError("Access identity is malformed")
        email = normalize_allowed_email(raw_email)
        subject = raw_subject.strip()
        if (
            not subject
            or subject != raw_subject
            or len(subject) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in subject)
            or email != self.allowed_email
        ):
            raise jwt.InvalidTokenError("Identity is not allowed")
        return AuthenticatedUser(email=email, subject=subject)


class IdentityMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: Any,
        settings: Settings,
        verifier: CloudflareAccessVerifier | None = None,
    ) -> None:
        super().__init__(app)
        self.settings = settings
        self.verifier = verifier or (
            CloudflareAccessVerifier(
                settings.cf_issuer or "",
                settings.cf_audience or "",
                settings.allowed_email or "",
            )
            if settings.auth_mode == "cloudflare"
            else None
        )

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if request.url.path == "/healthz" and not getattr(request.state, "public_origin", False):
            return await call_next(request)

        if self.settings.auth_mode == "local":
            request.state.user = AuthenticatedUser(email="local@northstar.invalid", subject="local-user")
            return await call_next(request)

        token = request.headers.get("Cf-Access-Jwt-Assertion", "")
        if not token or self.verifier is None:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        try:
            request.state.user = await self.verifier.verify(token)
        except (jwt.PyJWTError, OSError, TimeoutError, ValueError):
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        return await call_next(request)
