from __future__ import annotations

import os
import plistlib
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from northstar.operations.cloudflared import (
    CLOUDFLARED_ARTIFACTS,
    CLOUDFLARED_VERSION,
    cloudflared_artifact,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLIST = PROJECT_ROOT / "deployment/mac-mini/com.northstar.local.tunnel.plist"
RUNBOOK = PROJECT_ROOT / "deployment/mac-mini/CLOUDFLARE.md"
scan = runpy.run_path(str(PROJECT_ROOT / "scripts/scan_secrets.py"))["scan"]
scan_bytes = runpy.run_path(str(PROJECT_ROOT / "scripts/scan_secrets.py"))["scan_bytes"]


def _fake_cloudflared(path: Path) -> None:
    path.write_text(
        f"#!/bin/sh\nprintf 'cloudflared version {CLOUDFLARED_VERSION}\\n'\n",
        encoding="utf-8",
    )
    path.chmod(0o700)


def test_cloudflared_release_is_exactly_version_architecture_and_checksum_pinned() -> None:
    assert CLOUDFLARED_VERSION == "2026.7.3"
    assert cloudflared_artifact("arm64") == (
        "https://github.com/cloudflare/cloudflared/releases/download/2026.7.3/cloudflared-darwin-arm64.tgz",
        "90c5a4f914d705fd70c135dba6d80b1791d254b08d6d4136301941f88330dd09",
    )
    assert cloudflared_artifact("x86_64") == (
        "https://github.com/cloudflare/cloudflared/releases/download/2026.7.3/cloudflared-darwin-amd64.tgz",
        "70d1c8684fa6d14b5843787ec8d1ea8e18b23650e424f4ea43d849a506487c3b",
    )
    assert set(CLOUDFLARED_ARTIFACTS) == {"arm64", "x86_64"}


def test_tunnel_launchagent_is_credential_free_constrained_and_uses_token_file() -> None:
    content = PLIST.read_text(encoding="utf-8")
    payload = plistlib.loads(content.encode("utf-8"))
    arguments = payload["ProgramArguments"]

    assert arguments == [
        "__CLOUDFLARED_EXECUTABLE__",
        "tunnel",
        "--no-autoupdate",
        "--loglevel",
        "warn",
        "--transport-loglevel",
        "warn",
        "--metrics",
        "127.0.0.1:0",
        "run",
        "--token-file",
        "__TUNNEL_TOKEN_FILE__",
    ]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["Umask"] == 63
    assert payload["HardResourceLimits"] == {
        "NumberOfFiles": 256,
        "NumberOfProcesses": 32,
        "ResidentSetSize": 536870912,
    }
    assert "EnvironmentVariables" not in payload
    assert "StandardOutPath" not in payload
    assert "StandardErrorPath" not in payload
    assert "eyJ" not in content


def test_launchagent_installer_writes_paths_only_and_rejects_unsafe_token(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "cloudflared"
    _fake_cloudflared(binary)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    token_file = runtime / "tunnel-token"
    synthetic_payload = "synthetic-tunnel-credential-placeholder"
    token_file.write_text(synthetic_payload, encoding="utf-8")
    token_file.chmod(0o600)
    destination = tmp_path / "LaunchAgents"

    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts/install_cloudflared_launchd.py"),
        "--cloudflared",
        str(binary),
        "--token-file",
        str(token_file),
        "--destination",
        str(destination),
    ]
    installed = subprocess.run(command, check=False, capture_output=True, text=True)  # noqa: S603
    generated = destination / PLIST.name
    generated_content = generated.read_text(encoding="utf-8")

    assert installed.returncode == 0
    assert installed.stdout.strip() == "cloudflared_launchd_install: ok"
    assert generated.stat().st_mode & 0o777 == 0o600
    assert str(binary) in generated_content
    assert str(token_file) in generated_content
    assert synthetic_payload not in generated_content

    real_token = runtime / "real-token"
    token_file.rename(real_token)
    token_file.symlink_to(real_token)
    rejected = subprocess.run(command, check=False, capture_output=True, text=True)  # noqa: S603

    assert rejected.returncode == 1
    assert rejected.stdout.strip() == "cloudflared_launchd_install: fail"


def test_secret_scanner_rejects_tunnel_token_artifacts(tmp_path: Path) -> None:
    token_file = tmp_path / "tunnel.token"
    token_file.write_text("synthetic-placeholder", encoding="utf-8")

    relative = Path(os.path.relpath(token_file, PROJECT_ROOT))
    assert scan(relative) == ["forbidden sensitive filename"]


def test_history_scanner_uses_blob_content_without_a_worktree_file() -> None:
    reasons = scan_bytes(
        Path("deleted-sensitive.token"),
        b"synthetic-placeholder-that-is-not-a-real-credential",
    )

    assert reasons == ["forbidden sensitive filename"]


@pytest.mark.parametrize(
    "path",
    [
        Path("exports/synthetic-holdings.xlsx"),
        Path("exports/synthetic-transactions.tsv"),
        Path("exports/synthetic-account.qif"),
        Path("exports/account-statement.pdf"),
        Path("exports/tax-lots.json"),
        Path("exports/realized-gains.zip"),
    ],
)
def test_secret_scanner_rejects_common_financial_export_artifacts(path: Path) -> None:
    assert scan_bytes(path, b"synthetic-only") == ["forbidden sensitive filename"]


def test_container_disables_proxy_and_access_logging() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "--no-access-log" in dockerfile
    assert "--no-proxy-headers" in dockerfile
    assert "--no-server-header" in dockerfile
    assert "--forwarded-allow-ips" not in dockerfile


def test_runbook_preserves_the_fixed_remote_and_recovery_boundaries() -> None:
    content = RUNBOOK.read_text(encoding="utf-8")

    assert "https://northstar.example.com" in content
    assert "http://127.0.0.1:8010" in content
    assert "Create Access before a tunnel route" in content
    assert "exact single user email" in content
    assert "Require MFA" in content
    assert "`/integrations/schwab/callback`" in content
    assert "`/schwab/callback`" not in content
    assert "catch-all HTTP 404" in content
    assert "Cache-Control: no-store, private, max-age=0" in content
    assert "/Volumes/NorthstarRecovery/northstar-backups" in content
    assert "cloudflared` `2026.7.3`" in content
    assert "password-manager reconstructed" in content
    assert "Do not merge or deploy Gate 4B as complete" in content
