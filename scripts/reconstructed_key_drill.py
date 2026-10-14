#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hmac
import json
import os
import stat
import tempfile
from pathlib import Path

from northstar.operations import MonthlyRecoveryJob
from northstar.operations.config import KEY_NAMES, load_secrets_environment, validate_key_environment
from northstar.operations.runtime import load_operational_runtime

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RECOVERY_NAMES = (*KEY_NAMES, "NORTHSTAR_SESSION_KEY", "NORTHSTAR_KEY_VERSION")


def main() -> int:
    parser = argparse.ArgumentParser(description="Drill keys reconstructed from the password manager.")
    parser.add_argument("--production-secrets", required=True, type=Path)
    parser.add_argument("--reconstructed-secrets", required=True, type=Path)
    parser.add_argument("--delete-reconstructed", action="store_true", required=True)
    args = parser.parse_args()
    reconstructed = args.reconstructed_secrets.expanduser().absolute()
    try:
        production = args.production_secrets.expanduser().absolute()
        _validate_inputs(production, reconstructed)
        production_values = load_secrets_environment(production)
        reconstructed_values = load_secrets_environment(reconstructed)
        validate_key_environment(production_values)
        validate_key_environment(reconstructed_values)
        if not all(
            hmac.compare_digest(production_values.get(name, ""), reconstructed_values.get(name, ""))
            for name in RECOVERY_NAMES
        ):
            raise ValueError("Reconstructed recovery keys do not match")
        _run_isolated(production_values, reconstructed_values)
    except Exception:
        print(json.dumps({"status": "fail", "code": "reconstructed_key_drill_failed"}))
        return 1
    finally:
        _remove_reconstructed(reconstructed)
    print(
        json.dumps(
            {
                "status": "ok",
                "keys": "matched",
                "backup": "authenticated",
                "database": "encrypted",
                "fields": "authenticated",
                "schema": "compatible",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _validate_inputs(production: Path, reconstructed: Path) -> None:
    if production == reconstructed or any(
        path.resolve().is_relative_to(PROJECT_ROOT) for path in (production, reconstructed)
    ):
        raise ValueError("Recovery secret path is unsafe")
    for path in (production, reconstructed):
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or not metadata.st_mode & stat.S_IRUSR
        ):
            raise ValueError("Recovery secret permissions are unsafe")


def _run_isolated(production: dict[str, str], reconstructed: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory(prefix="northstar-reconstructed-key-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        operations = root / "operations"
        operations.mkdir(mode=0o700)
        combined = dict(production)
        combined.update({name: reconstructed[name] for name in RECOVERY_NAMES})
        combined["NORTHSTAR_RUNTIME_ROOT"] = str(root)
        combined["NORTHSTAR_OPERATIONS_DIR"] = str(operations)
        combined_path = root / "secrets.env"
        combined["NORTHSTAR_SECRETS_PATH"] = str(combined_path)
        descriptor = os.open(combined_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for name, value in combined.items():
                handle.write(f"{name}={value}\n")
            handle.flush()
            os.fsync(handle.fileno())
        combined_path.chmod(0o600)
        runtime = load_operational_runtime(combined_path, repository_root=PROJECT_ROOT)
        MonthlyRecoveryJob(
            runtime.backup_service,
            runtime.state,
            runtime.operational.operations_dir,
        ).run()


def _remove_reconstructed(path: Path) -> None:
    if not os.path.lexists(path):
        return
    try:
        metadata = path.lstat()
    except OSError:
        return
    if (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
    ):
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
