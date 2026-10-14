"""Reviewed cloudflared release metadata for the Mac mini deployment boundary."""

from __future__ import annotations

import platform

CLOUDFLARED_VERSION = "2026.7.3"
CLOUDFLARED_ARTIFACTS = {
    "arm64": (
        "cloudflared-darwin-arm64.tgz",
        "90c5a4f914d705fd70c135dba6d80b1791d254b08d6d4136301941f88330dd09",
    ),
    "x86_64": (
        "cloudflared-darwin-amd64.tgz",
        "70d1c8684fa6d14b5843787ec8d1ea8e18b23650e424f4ea43d849a506487c3b",
    ),
}


def cloudflared_artifact(machine: str | None = None) -> tuple[str, str]:
    """Return the exact release URL and digest for a supported Mac architecture."""
    architecture = machine or platform.machine()
    try:
        filename, digest = CLOUDFLARED_ARTIFACTS[architecture]
    except KeyError as error:
        raise ValueError("Pinned cloudflared build requires a supported Mac architecture") from error
    return (
        f"https://github.com/cloudflare/cloudflared/releases/download/{CLOUDFLARED_VERSION}/{filename}",
        digest,
    )


__all__ = [
    "CLOUDFLARED_ARTIFACTS",
    "CLOUDFLARED_VERSION",
    "cloudflared_artifact",
]
