#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from northstar.operations import ReplicationService
from northstar.operations.runtime import load_operational_runtime

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Replicate finalized encrypted Northstar backups.")
    parser.add_argument("--secrets-file", required=True, type=Path)
    parser.add_argument("--destination", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    runtime = None
    try:
        runtime = load_operational_runtime(args.secrets_file.expanduser(), repository_root=PROJECT_ROOT)
        destination = (
            args.destination.expanduser().absolute()
            if args.destination is not None
            else runtime.operational.replication_dir
        )
        if destination is None:
            raise ValueError("Replication destination is not configured")
        result = ReplicationService(
            runtime.settings.backup_dir,
            destination,
            runtime.settings.backup_vault(),
            repository_root=PROJECT_ROOT,
            runtime_root=runtime.operational.runtime_root,
        ).run(dry_run=args.dry_run, verify_only=args.verify_only)
        if not args.dry_run:
            runtime.state.success("replication")
    except Exception:
        if runtime is not None:
            runtime.state.failure("replication_command_failed")
        print(json.dumps({"status": "fail", "code": "replication_command_failed"}))
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "mode": "dry_run" if args.dry_run else "verify" if args.verify_only else "copy",
                "source_count": result.source_count,
                "copied_count": result.copied_count,
                "verified_count": result.verified_count,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
