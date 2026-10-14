from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request

SESSION_KEY = "csrf_token"


def csrf_token(request: Request) -> str:
    token = request.session.get(SESSION_KEY)
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_KEY] = token
    return token


async def require_csrf(request: Request) -> None:
    expected = request.session.get(SESSION_KEY)
    supplied = request.headers.get("X-CSRF-Token")
    if supplied is None:
        form = await request.form()
        supplied = str(form.get("csrf_token", ""))
    if not isinstance(expected, str) or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="Request verification failed")
