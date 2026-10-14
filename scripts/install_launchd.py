#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import plistlib
import stat
import tempfile
from pathlib import Path

from northstar.operations.config import load_secrets_environment

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = (
    "com.northstar.local.backup.plist",
    "com.northstar.local.recovery.plist",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install credential-free Northstar launchd schedules.")
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--secrets-file", required=True, type=Path)
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("~/Library/LaunchAgents"),
    )
    args = parser.parse_args()
    try:
        # Preserve the virtual-environment entry point. Resolving this symlink to
        # the base interpreter would make launchd lose the venv's dependencies.
        python = _validated_executable_path(args.python)
        supplied_secrets = args.secrets_file.expanduser()
        load_secrets_environment(supplied_secrets)
        secrets = supplied_secrets.resolve(strict=True)
        destination = args.destination.expanduser().absolute()
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = destination.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise ValueError("LaunchAgent destination permissions are unsafe")
        replacements = {
            "__PYTHON_EXECUTABLE__": str(python),
            "__PROJECT_ROOT__": str(PROJECT_ROOT),
            "__SECRETS_FILE__": str(secrets),
        }
        for name in TEMPLATES:
            template = PROJECT_ROOT / "deployment" / "mac-mini" / name
            content = template.read_text(encoding="utf-8")
            for marker, value in replacements.items():
                if any(character in value for character in ("\n", "\r", "\x00")):
                    raise ValueError("LaunchAgent path contains an unsafe character")
                content = content.replace(marker, value)
            plistlib.loads(content.encode("utf-8"))
            _atomic_write(destination / name, content.encode("utf-8"))
    except Exception:
        print("launchd_install: fail")
        return 1
    print("launchd_install: ok")
    return 0


def _validated_executable_path(supplied: Path) -> Path:
    executable = supplied.expanduser().absolute()
    executable.resolve(strict=True)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("Python executable is unavailable")
    return executable


def _atomic_write(destination: Path, payload: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        destination.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
