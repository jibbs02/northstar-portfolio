#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import plistlib
import stat
import subprocess
import tempfile
from pathlib import Path

from northstar.operations.cloudflared import CLOUDFLARED_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = PROJECT_ROOT / "deployment/mac-mini/com.northstar.local.tunnel.plist"


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the credential-free cloudflared LaunchAgent.")
    parser.add_argument("--cloudflared", required=True, type=Path)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--destination", type=Path, default=Path("~/Library/LaunchAgents"))
    args = parser.parse_args()
    try:
        binary = args.cloudflared.expanduser().absolute()
        token_file = args.token_file.expanduser().absolute()
        destination = args.destination.expanduser().absolute()
        _validate_binary(binary)
        _validate_token_file(token_file)
        _validate_destination(destination)
        replacements = {
            "__CLOUDFLARED_EXECUTABLE__": str(binary),
            "__TUNNEL_TOKEN_FILE__": str(token_file),
        }
        content = TEMPLATE.read_text(encoding="utf-8")
        for marker, value in replacements.items():
            if any(character in value for character in ("\n", "\r", "\x00")):
                raise ValueError("LaunchAgent path contains an unsafe character")
            content = content.replace(marker, value)
        plistlib.loads(content.encode("utf-8"))
        _atomic_write(destination / TEMPLATE.name, content.encode("utf-8"))
    except Exception:
        print("cloudflared_launchd_install: fail")
        return 1
    print("cloudflared_launchd_install: ok")
    return 0


def _validate_binary(binary: Path) -> None:
    metadata = binary.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or not metadata.st_mode & stat.S_IXUSR
        or binary.resolve().is_relative_to(PROJECT_ROOT)
    ):
        raise ValueError("cloudflared binary is unsafe")
    result = subprocess.run(  # noqa: S603
        [binary, "version"], check=False, capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0 or f"cloudflared version {CLOUDFLARED_VERSION}" not in result.stdout:
        raise ValueError("cloudflared binary version does not match")


def _validate_token_file(token_file: Path) -> None:
    metadata = token_file.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
        or not metadata.st_mode & stat.S_IRUSR
        or not 1 <= metadata.st_size <= 16 * 1024
        or token_file.resolve().is_relative_to(PROJECT_ROOT)
    ):
        raise ValueError("Tunnel token file is unsafe")


def _validate_destination(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = destination.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or destination.resolve().is_relative_to(PROJECT_ROOT)
    ):
        raise ValueError("LaunchAgent destination is unsafe")


def _atomic_write(destination: Path, payload: bytes) -> None:
    if os.path.lexists(destination):
        metadata = destination.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise ValueError("Existing LaunchAgent is unsafe")
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
