#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from northstar.operations import MonthlyRecoveryJob
from northstar.operations.runtime import load_operational_runtime

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an isolated monthly encrypted recovery drill.")
    parser.add_argument("--secrets-file", required=True, type=Path)
    args = parser.parse_args()
    runtime = None
    try:
        runtime = load_operational_runtime(args.secrets_file.expanduser(), repository_root=PROJECT_ROOT)
        MonthlyRecoveryJob(
            runtime.backup_service,
            runtime.state,
            runtime.operational.operations_dir,
        ).run()
    except Exception:
        if runtime is not None:
            runtime.state.failure("recovery_command_failed")
        print(json.dumps({"status": "fail", "code": "recovery_command_failed"}))
        return 1
    print(json.dumps({"status": "ok", "schema": "compatible", "records": "authenticated"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
