from __future__ import annotations

from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from northstar.config import Settings

MAX_IMPORT_REQUEST_BYTES = 2 * 1024 * 1024 + 64 * 1024


class ImportRequestLimitMiddleware:
    """Reject import bodies before multipart parsing can write a temporary file."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") != "/imports/preview":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            length = -1
        if length < 0 or length > MAX_IMPORT_REQUEST_BYTES:
            response = Response("Import request is too large", status_code=413)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class OriginBoundaryMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if self.settings.environment != "production":
            return await call_next(request)
        host = request.headers.get("host", "").strip().casefold()
        public_origin = host == self.settings.production_hostname
        request.state.public_origin = public_origin
        if public_origin:
            forwarded_host = request.headers.get("x-forwarded-host")
            forwarded_proto = request.headers.get("x-forwarded-proto")
            if request.headers.get("forwarded") is not None:
                return Response("Invalid proxy metadata", status_code=400)
            if forwarded_host is not None and forwarded_host.strip().casefold() != host:
                return Response("Invalid proxy metadata", status_code=400)
            if forwarded_proto is not None and forwarded_proto.strip().casefold() != "https":
                return Response("Invalid proxy metadata", status_code=400)
            return await call_next(request)
        if request.url.path == "/healthz" and _local_health_host(host):
            return await call_next(request)
        if _local_health_host(host):
            return Response("Authentication required", status_code=401)
        return Response("Invalid request host", status_code=400)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; object-src 'none'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store, private, max-age=0"
        response.headers["Pragma"] = "no-cache"
        if self.settings.environment == "production" and getattr(request.state, "public_origin", False):
            response.headers["Strict-Transport-Security"] = "max-age=86400"
        return response


def _local_health_host(host: str) -> bool:
    if host in {"127.0.0.1", "localhost"}:
        return True
    name, separator, port = host.rpartition(":")
    return bool(
        separator and port.isdigit() and 1 <= int(port) <= 65535 and name in {"127.0.0.1", "localhost"}
    )
