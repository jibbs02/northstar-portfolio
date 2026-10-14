#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from northstar.data import ProviderConnectionRepository
from northstar.integrations import HttpSchwabOAuthClient, HttpSchwabReadClient
from northstar.operations import (
    ExclusiveJobLock,
    LiveSnapshotAuthorizationStore,
    LiveSnapshotOperator,
    ProductionPreflight,
)
from northstar.operations.runtime import load_operational_runtime

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIRMATION = "I manually reconfirmed the Schwab order throttle is zero"
RECOVERY_MAX_AGE = timedelta(minutes=30)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Authorize or execute one local, read-only Schwab snapshot operation."
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    authorize = subparsers.add_parser("authorize")
    authorize.add_argument("--expected-mappings", required=True, type=int)
    subparsers.add_parser("execute")
    args = parser.parse_args()
    try:
        runtime = _runtime()
        if args.action == "authorize":
            return _authorize(runtime.operational.operations_dir, args.expected_mappings)
        return asyncio.run(_execute(runtime))
    except Exception:
        print(json.dumps({"status": "fail", "code": "live_snapshot_entrypoint_failed"}))
        return 1


def _runtime():
    value = os.environ.get("NORTHSTAR_SECRET_FILE", "").strip()
    if not value:
        raise ValueError("NORTHSTAR_SECRET_FILE is required")
    return load_operational_runtime(Path(value).expanduser(), repository_root=PROJECT_ROOT)


def _authorize(operations_dir: Path, expected_mapping_count: int) -> int:
    if not sys.stdin.isatty():
        raise ValueError("Interactive confirmation is required")
    entered = input(f'Type exactly "{CONFIRMATION}" to create one short-lived authorization:\n')
    if entered != CONFIRMATION:
        raise ValueError("Order-throttle confirmation did not match")
    with ExclusiveJobLock(operations_dir, "schwab-live-snapshot"):
        LiveSnapshotAuthorizationStore(operations_dir).create(expected_mapping_count)
    print(json.dumps({"authorization": "ready", "expires_in_minutes": 15}))
    return 0


async def _execute(runtime) -> int:
    deployed_commit = _deployed_commit(runtime.operational.container_name)
    provider_secret = runtime.settings.schwab_client_secret or ""
    oauth = HttpSchwabOAuthClient(
        client_id=runtime.settings.schwab_client_id or "",
        client_secret=provider_secret,
        redirect_uri=runtime.settings.schwab_redirect_uri or "",
    )
    reads = HttpSchwabReadClient()
    try:
        operator = LiveSnapshotOperator(
            database=runtime.database,
            connections=ProviderConnectionRepository(runtime.database),
            oauth_client=oauth,
            read_client=reads,
            operations_dir=runtime.operational.operations_dir,
            deployed_commit=deployed_commit,
            compatibility_check=lambda: _compatible(runtime, deployed_commit),
            recovery_check=lambda confirmed_at: _recovery_checkpoint(runtime, confirmed_at),
        )
        result = await operator.run()
    finally:
        await oauth.close()
        await reads.close()
    print(json.dumps(result.safe_dict(), separators=(",", ":"), sort_keys=True))
    return 0 if result.atomic_publication == "published" and result.reconciliation == "pass" else 1


def _compatible(runtime, deployed_commit: str) -> bool:
    try:
        git = _binary("git")
        head = _run([git, "rev-parse", "HEAD"], cwd=PROJECT_ROOT)
        checkout = _run([git, "status", "--porcelain"], cwd=PROJECT_ROOT)
        local_commit = head.stdout.strip().lower()
        runtime.database.verify_critical_invariants()
        return (
            head.returncode == 0
            and checkout.returncode == 0
            and not checkout.stdout.strip()
            and local_commit == deployed_commit
            and runtime.state.read().pending_recovery_schema_version is None
            and ProductionPreflight(runtime.settings, runtime.operational).run().passed
        )
    except Exception:
        return False


def _recovery_checkpoint(runtime, confirmed_at: datetime) -> bool:
    try:
        latest = runtime.backup_service.latest()
        if latest is None:
            return False
        created_at = runtime.backup_service.creation_time(latest)
        now = datetime.now(UTC)
        if created_at <= confirmed_at or created_at > now or now - created_at > RECOVERY_MAX_AGE:
            return False
        runtime.backup_service.restore_drill(latest)
        runtime.state.success("restore_drill")
        return True
    except Exception:
        return False


def _deployed_commit(container_name: str) -> str:
    result = _run(
        [
            _binary("docker"),
            "inspect",
            "--format",
            '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            container_name,
        ],
        cwd=PROJECT_ROOT,
    )
    if result.returncode != 0:
        raise ValueError("Deployed image identity is unavailable")
    return result.stdout.strip().lower()


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _binary(name: str) -> str:
    value = shutil.which(name)
    if value is None:
        raise RuntimeError("Required operator command is unavailable")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
