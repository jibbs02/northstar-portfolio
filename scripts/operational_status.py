#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from northstar.operations.runtime import load_operational_runtime
from northstar.operations.status import collect_operational_status

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Show privacy-safe Northstar operational status.")
    parser.add_argument("--secrets-file", required=True, type=Path)
    args = parser.parse_args()
    try:
        runtime = load_operational_runtime(args.secrets_file.expanduser(), repository_root=PROJECT_ROOT)
        status = collect_operational_status(runtime)
    except Exception:
        print(json.dumps({"status": "unavailable", "code": "status_collection_failed"}))
        return 1
    print(json.dumps(status.safe_dict(), separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
