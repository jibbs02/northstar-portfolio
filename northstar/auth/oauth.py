from __future__ import annotations

import secrets

from starlette.requests import Request

OAUTH_BINDING_FIELD = "oauth_session_binding"
MAX_OAUTH_CALLBACK_QUERY_BYTES = 16 * 1024


def oauth_session_binding(request: Request) -> str:
    binding = request.session.get(OAUTH_BINDING_FIELD)
    if not isinstance(binding, str) or len(binding) < 32:
        binding = secrets.token_urlsafe(32)
        request.session[OAUTH_BINDING_FIELD] = binding
    return binding


def clear_oauth_session(request: Request, *, rotate_session: bool = False) -> None:
    if rotate_session:
        request.session.clear()
    else:
        request.session.pop(OAUTH_BINDING_FIELD, None)
