#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from northstar.operations.preflight import ProductionPreflight
from northstar.operations.runtime import load_operational_runtime

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run fail-closed Northstar production preflight.")
    parser.add_argument("--secrets-file", required=True, type=Path)
    args = parser.parse_args()
    try:
        runtime = load_operational_runtime(args.secrets_file.expanduser(), repository_root=PROJECT_ROOT)
        report = ProductionPreflight(runtime.settings, runtime.operational).run()
    except Exception:
        print(json.dumps({"status": "fail", "code": "preflight_configuration_invalid"}))
        return 1
    print(json.dumps(report.safe_dict(), separators=(",", ":"), sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
