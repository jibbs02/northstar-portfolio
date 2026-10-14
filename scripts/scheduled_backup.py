#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from northstar.operations import ReplicationService, ScheduledBackupJob
from northstar.operations.runtime import load_operational_runtime

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create, drill, retain, and optionally replicate an encrypted backup."
    )
    parser.add_argument("--secrets-file", required=True, type=Path)
    args = parser.parse_args()
    runtime = None
    try:
        runtime = load_operational_runtime(args.secrets_file.expanduser(), repository_root=PROJECT_ROOT)
        replication = (
            ReplicationService(
                runtime.settings.backup_dir,
                runtime.operational.replication_dir,
                runtime.settings.backup_vault(),
                repository_root=PROJECT_ROOT,
                runtime_root=runtime.operational.runtime_root,
            )
            if runtime.operational.replication_dir is not None
            else None
        )
        result = ScheduledBackupJob(
            runtime.backup_service,
            runtime.state,
            runtime.operational.operations_dir,
            replication=replication,
        ).run()
    except Exception:
        if runtime is not None:
            runtime.state.failure("backup_command_failed")
        print(json.dumps({"status": "fail", "code": "backup_command_failed"}))
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "backup_count": result.backup_count,
                "replication": "complete" if result.replication is not None else "not_configured",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
