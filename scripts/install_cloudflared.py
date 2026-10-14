#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path

from northstar.operations.cloudflared import (
    CLOUDFLARED_VERSION,
    cloudflared_artifact,
)

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_BINARY_BYTES = 128 * 1024 * 1024
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the reviewed pinned cloudflared build.")
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path(f"~/.local/bin/cloudflared-{CLOUDFLARED_VERSION}"),
    )
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        destination = args.destination.expanduser().absolute()
        _validate_platform()
        _validate_destination(destination, must_exist=args.verify_only)
        if args.verify_only:
            _verify_binary(destination)
        else:
            _download_and_install(destination)
            _verify_binary(destination)
    except Exception:
        print("cloudflared_install: fail")
        return 1
    print(f"cloudflared_install: ok version={CLOUDFLARED_VERSION}")
    return 0


def _validate_platform() -> None:
    if platform.system() != "Darwin":
        raise ValueError("Pinned cloudflared build requires macOS")
    cloudflared_artifact()


def _validate_destination(destination: Path, *, must_exist: bool) -> None:
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    parent_metadata = parent.lstat()
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.getuid()
        or parent_metadata.st_mode & 0o022
        or parent.resolve().is_relative_to(PROJECT_ROOT)
    ):
        raise ValueError("cloudflared destination is unsafe")
    exists = os.path.lexists(destination)
    if must_exist != exists:
        raise ValueError("cloudflared destination state is unexpected")
    if exists:
        metadata = destination.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022
        ):
            raise ValueError("cloudflared binary is unsafe")


def _download_and_install(destination: Path) -> None:
    archive_url, archive_sha256 = cloudflared_artifact()
    with tempfile.TemporaryDirectory(prefix="northstar-cloudflared-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        archive = root / "cloudflared.tgz"
        _download(archive, archive_url)
        if _hash_file(archive) != archive_sha256:
            raise ValueError("cloudflared archive checksum does not match")
        _extract_binary(archive, destination)


def _download(destination: Path, archive_url: str) -> None:
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        result = subprocess.run(  # noqa: S603
            [
                "/usr/bin/curl",
                "--fail",
                "--location",
                "--proto",
                "=https",
                "--tlsv1.2",
                "--silent",
                "--show-error",
                "--max-filesize",
                str(MAX_ARCHIVE_BYTES),
                "--user-agent",
                "Northstar-Cloudflared-Installer/1",
                "--output",
                str(destination),
                archive_url,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise ValueError("cloudflared download failed")
        size = destination.stat().st_size
        if not 1 <= size <= MAX_ARCHIVE_BYTES:
            raise ValueError("cloudflared archive size is invalid")
        with destination.open("rb") as handle:
            os.fsync(handle.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _extract_binary(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, mode="r:gz") as bundle:
        members = [member for member in bundle.getmembers() if member.isfile()]
        if (
            len(members) != 1
            or Path(members[0].name).name != "cloudflared"
            or not 1 <= members[0].size <= MAX_BINARY_BYTES
        ):
            raise ValueError("cloudflared archive contents are unexpected")
        source = bundle.extractfile(members[0])
        if source is None:
            raise ValueError("cloudflared binary is unavailable")
        descriptor, name = tempfile.mkstemp(prefix=".cloudflared.", dir=destination.parent)
        temporary = Path(name)
        total = 0
        try:
            with source, os.fdopen(descriptor, "wb") as handle:
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_BINARY_BYTES:
                        raise ValueError("cloudflared binary exceeds the size limit")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o700)
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)


def _verify_binary(binary: Path) -> None:
    result = subprocess.run(  # noqa: S603
        [binary, "version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0 or f"cloudflared version {CLOUDFLARED_VERSION}" not in result.stdout:
        raise ValueError("cloudflared binary version does not match")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
