from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from northstar.data.connections import (
    ProviderConnectionRepository,
    ProviderConnectionStateError,
)
from northstar.integrations import SchwabOAuthClient, SchwabOAuthError, SchwabReadClient

from .schwab_snapshots import (
    SCHWAB_HTTP_SNAPSHOT_PROVENANCE,
    SchwabSnapshotService,
    SnapshotMappingSet,
    SnapshotPublication,
)

ACCESS_TOKEN_REFRESH_SKEW = timedelta(minutes=1)


class SchwabSnapshotCoordinator:
    """Connect encrypted grants and mapped accounts to the dormant read-only snapshot path.

    The coordinator has no route, scheduler, startup hook, or other invocation path. A caller must
    explicitly supply a connection and actor. The snapshot service owns failure recording and atomic
    publication; this class owns one-token-per-batch loading and refresh-token race resolution.
    """

    def __init__(
        self,
        *,
        connections: ProviderConnectionRepository,
        oauth_client: SchwabOAuthClient,
        read_client: SchwabReadClient,
        snapshots: SchwabSnapshotService,
    ) -> None:
        self.connections = connections
        self.oauth_client = oauth_client
        self.read_client = read_client
        self.snapshots = snapshots

    async def refresh(
        self,
        *,
        connection_id: str,
        actor: str,
        mapping_set: SnapshotMappingSet | None = None,
        before_provider_work: Callable[[], None] | None = None,
    ) -> SnapshotPublication:
        access_task: asyncio.Task[str] | None = None
        batch_observed_at = datetime.now(UTC)

        async def load_snapshot(provider_account_hash: str):
            nonlocal access_task
            if before_provider_work is not None:
                before_provider_work()
            if access_task is None:
                access_task = asyncio.create_task(
                    self._usable_access_token(connection_id),
                    name="schwab-snapshot-token-acquisition",
                )
            # A loader cancellation must not cancel the shared acquisition while another mapped
            # account is awaiting it. The outer finally block owns cancellation and retrieval.
            token = await asyncio.shield(access_task)
            if before_provider_work is not None:
                before_provider_work()
            snapshot = await self.read_client.account_snapshot(
                access_token=token,
                provider_account_hash=provider_account_hash,
            )
            # HTTP observations in one fan-out complete at slightly different instants. The
            # publication timestamp represents the explicitly started, all-or-nothing batch.
            return replace(snapshot, observed_at_utc=batch_observed_at)

        try:
            return await self.snapshots.refresh(
                connection_id=connection_id,
                actor=actor,
                load_snapshot=load_snapshot,
                provenance=SCHWAB_HTTP_SNAPSHOT_PROVENANCE,
                mapping_set=mapping_set,
            )
        finally:
            if access_task is not None:
                if not access_task.done():
                    access_task.cancel()
                await asyncio.gather(access_task, return_exceptions=True)

    async def _usable_access_token(self, connection_id: str) -> str:
        try:
            grant = self.connections.token_grant(connection_id)
        except (LookupError, ValueError, ProviderConnectionStateError):
            raise SchwabOAuthError("provider_authorization_rejected") from None
        if grant is None:
            raise SchwabOAuthError("provider_authorization_rejected")
        if grant.expires_at > datetime.now(UTC) + ACCESS_TOKEN_REFRESH_SKEW:
            return grant.access_token
        if grant.refresh_expires_at is None or grant.refresh_expires_at <= datetime.now(UTC):
            raise SchwabOAuthError("provider_authorization_rejected")

        refreshed = await self.oauth_client.refresh_access(refresh_token=grant.refresh_token)
        try:
            replaced = self.connections.replace_token_grant_if_current(
                connection_id,
                expected_refresh_token=grant.refresh_token,
                grant=refreshed,
            )
            if replaced:
                return refreshed.access_token
            winner = self.connections.token_grant(connection_id)
        except (LookupError, ValueError, ProviderConnectionStateError):
            raise SchwabOAuthError("provider_authorization_rejected") from None
        if winner is None or winner.expires_at <= datetime.now(UTC):
            raise SchwabOAuthError("provider_authorization_rejected")
        return winner.access_token
