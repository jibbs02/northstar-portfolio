from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from northstar.integrations import SchwabAccountReference, SchwabOAuthError

PREVIEW_TTL = timedelta(minutes=5)
MAX_DISCOVERY_ATTEMPTS = 2
RETRYABLE_DISCOVERY_CODES = frozenset({"provider_rate_limited", "provider_unavailable"})


class DiscoveryError(ValueError):
    """A privacy-safe discovery failure suitable for an audit result."""


@dataclass(frozen=True)
class DiscoveryPreview:
    token: str = field(repr=False)
    connection_id: str = field(repr=False)
    accounts: tuple[SchwabAccountReference, ...] = field(repr=False)
    expires_at: datetime


class SchwabDiscoveryService:
    """Ephemeral, per-connection previews. Nothing is persisted before confirmation."""

    def __init__(self) -> None:
        self._previews: dict[str, DiscoveryPreview] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._invalidated_connections: set[str] = set()

    async def discover(
        self,
        *,
        connection_id: str,
        load_accounts: Callable[[], Awaitable[tuple[SchwabAccountReference, ...]]],
    ) -> DiscoveryPreview:
        lock = self._locks.setdefault(connection_id, asyncio.Lock())
        if lock.locked():
            raise DiscoveryError("discovery_in_progress")
        async with lock:
            self._invalidated_connections.discard(connection_id)
            self._remove_previews(connection_id)
            accounts = await self._load_with_retry(load_accounts)
            if connection_id in self._invalidated_connections:
                raise DiscoveryError("preview_invalid")
            self._expire()
            token = secrets.token_urlsafe(32)
            preview = DiscoveryPreview(
                token=token,
                connection_id=connection_id,
                accounts=accounts,
                expires_at=datetime.now(UTC) + PREVIEW_TTL,
            )
            self._previews[token] = preview
            return preview

    async def _load_with_retry(
        self, load_accounts: Callable[[], Awaitable[tuple[SchwabAccountReference, ...]]]
    ) -> tuple[SchwabAccountReference, ...]:
        for attempt in range(MAX_DISCOVERY_ATTEMPTS):
            try:
                return await load_accounts()
            except SchwabOAuthError as exc:
                if exc.code not in RETRYABLE_DISCOVERY_CODES or attempt + 1 == MAX_DISCOVERY_ATTEMPTS:
                    raise DiscoveryError(exc.code) from None
                await asyncio.sleep(0.1 * (2**attempt))
        raise AssertionError("bounded discovery retry loop did not return")

    def consume(self, *, token: str, connection_id: str) -> DiscoveryPreview:
        self._expire()
        preview = self._previews.pop(token, None)
        if preview is None or preview.connection_id != connection_id:
            raise DiscoveryError("preview_invalid")
        return preview

    def invalidate(self, connection_id: str) -> None:
        self._invalidated_connections.add(connection_id)
        self._remove_previews(connection_id)

    def _remove_previews(self, connection_id: str) -> None:
        for token, preview in tuple(self._previews.items()):
            if preview.connection_id == connection_id:
                del self._previews[token]

    def _expire(self) -> None:
        now = datetime.now(UTC)
        for token, preview in tuple(self._previews.items()):
            if preview.expires_at <= now:
                del self._previews[token]
