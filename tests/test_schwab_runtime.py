from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

import pytest

from northstar.operations.config import (
    KEY_NAMES,
    apply_environment,
    load_secrets_environment,
    validate_schwab_environment,
)
from northstar.operations.runtime import load_operational_runtime

PROJECT_ROOT = Path(__file__).resolve().parents[1]
runtime_creator = runpy.run_path(str(PROJECT_ROOT / "scripts/create_production_runtime.py"))
schwab_configurator = runpy.run_path(str(PROJECT_ROOT / "scripts/configure_schwab_runtime.py"))
create_runtime = runtime_creator["_create"]
configure_schwab = schwab_configurator["_configure"]
disable_schwab = schwab_configurator["_disable"]


def _runtime(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "production"
    secrets_path = root / "secrets.env"
    create_runtime(
        root,
        secrets_path,
        "https://synthetic-team.cloudflareaccess.com",
        "synthetic-audience",
        "synthetic-user@example.invalid",
    )
    return secrets_path, load_secrets_environment(secrets_path)


def _client_values() -> tuple[str, str]:
    client_id = "-".join(("synthetic", "placeholder", "client", "identifier"))
    client_secret = "-".join(("synthetic", "placeholder", "client", "secret")) + "-$#'\""
    return client_id, client_secret


def test_schwab_runtime_configuration_is_atomic_external_and_owner_only(tmp_path: Path) -> None:
    secrets_path, before = _runtime(tmp_path)
    client_id, client_secret = _client_values()

    configure_schwab(
        secrets_path,
        client_id=client_id,
        client_secret=client_secret,
        order_throttle_verified=True,
    )
    after = load_secrets_environment(secrets_path)

    assert secrets_path.stat().st_mode & 0o777 == 0o600
    assert all(after[name] == before[name] for name in KEY_NAMES)
    assert after["NORTHSTAR_SCHWAB_ENABLED"] == "1"
    assert after["NORTHSTAR_SCHWAB_ORDER_THROTTLE_VERIFIED"] == "1"
    assert after["SCHWAB_CLIENT_ID"] == client_id
    assert after["SCHWAB_CLIENT_SECRET"] == client_secret
    assert after["SCHWAB_REDIRECT_URI"] == (
        "https://northstar.example.com/integrations/schwab/callback"
    )
    assert not list(secrets_path.parent.glob(".schwab-secrets-*"))
    validate_schwab_environment(after)


def test_interrupted_schwab_configuration_preserves_the_original_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_path, _ = _runtime(tmp_path)
    original = secrets_path.read_bytes()
    client_id, client_secret = _client_values()

    def interrupt(_: Path, __: Path) -> None:
        raise OSError("synthetic interruption")

    monkeypatch.setattr(schwab_configurator["os"], "replace", interrupt)

    with pytest.raises(OSError, match="synthetic interruption"):
        configure_schwab(
            secrets_path,
            client_id=client_id,
            client_secret=client_secret,
            order_throttle_verified=True,
        )

    assert secrets_path.read_bytes() == original
    assert not list(secrets_path.parent.glob(".schwab-secrets-*"))


def test_schwab_runtime_disable_removes_provider_credentials(tmp_path: Path) -> None:
    secrets_path, _ = _runtime(tmp_path)
    client_id, client_secret = _client_values()
    configure_schwab(
        secrets_path,
        client_id=client_id,
        client_secret=client_secret,
        order_throttle_verified=True,
    )

    disable_schwab(secrets_path)
    values = load_secrets_environment(secrets_path)

    assert values["NORTHSTAR_SCHWAB_ENABLED"] == "0"
    assert values["NORTHSTAR_SCHWAB_ORDER_THROTTLE_VERIFIED"] == "0"
    assert "SCHWAB_CLIENT_ID" not in values
    assert "SCHWAB_CLIENT_SECRET" not in values
    assert "SCHWAB_REDIRECT_URI" not in values


def test_runtime_environment_applies_only_the_exact_schwab_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_id, client_secret = _client_values()
    values = {
        "NORTHSTAR_SCHWAB_ENABLED": "1",
        "NORTHSTAR_SCHWAB_ORDER_THROTTLE_VERIFIED": "1",
        "SCHWAB_CLIENT_ID": client_id,
        "SCHWAB_CLIENT_SECRET": client_secret,
        "SCHWAB_REDIRECT_URI": "https://northstar.example.com/integrations/schwab/callback",
        "SCHWAB_ORDER_ENDPOINT": "https://synthetic.invalid/orders",
    }
    for name in values:
        monkeypatch.delenv(name, raising=False)

    apply_environment(values)

    assert os.environ["NORTHSTAR_SCHWAB_ENABLED"] == "1"
    assert os.environ["SCHWAB_CLIENT_ID"] == client_id
    assert os.environ["SCHWAB_CLIENT_SECRET"] == client_secret
    assert os.environ["SCHWAB_REDIRECT_URI"].endswith("/integrations/schwab/callback")
    assert "SCHWAB_ORDER_ENDPOINT" not in os.environ


def test_host_operational_runtime_validates_the_enabled_schwab_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_path, _ = _runtime(tmp_path)
    client_id, client_secret = _client_values()
    configure_schwab(
        secrets_path,
        client_id=client_id,
        client_secret=client_secret,
        order_throttle_verified=True,
    )
    monkeypatch.setattr(os, "environ", {})

    runtime = load_operational_runtime(secrets_path, repository_root=PROJECT_ROOT)

    assert runtime.settings.schwab_enabled is True
    assert runtime.settings.schwab_client_id == client_id
    assert runtime.settings.schwab_client_secret == client_secret
    assert runtime.settings.schwab_redirect_uri == (
        "https://northstar.example.com/integrations/schwab/callback"
    )


@pytest.mark.parametrize(
    "update",
    [
        {"NORTHSTAR_SCHWAB_ENABLED": "true"},
        {"NORTHSTAR_SCHWAB_ORDER_THROTTLE_VERIFIED": "0"},
        {"NORTHSTAR_SCHWAB_ORDER_THROTTLE_VERIFIED": ""},
        {"SCHWAB_CLIENT_ID": ""},
        {"SCHWAB_CLIENT_SECRET": ""},
        {"SCHWAB_REDIRECT_URI": "https://northstar.example.com/wrong"},
    ],
)
def test_enabled_schwab_runtime_rejects_malformed_values(update: dict[str, str]) -> None:
    client_id, client_secret = _client_values()
    values = {
        "NORTHSTAR_SCHWAB_ENABLED": "1",
        "NORTHSTAR_SCHWAB_ORDER_THROTTLE_VERIFIED": "1",
        "SCHWAB_CLIENT_ID": client_id,
        "SCHWAB_CLIENT_SECRET": client_secret,
        "SCHWAB_REDIRECT_URI": "https://northstar.example.com/integrations/schwab/callback",
    }

    with pytest.raises(ValueError):
        validate_schwab_environment(values | update)


def test_configuration_command_uses_hidden_input_and_never_prints_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secrets_path, _ = _runtime(tmp_path)
    client_id, client_secret = _client_values()
    answers = iter((client_id, client_secret))
    monkeypatch.setattr("builtins.input", lambda _: "ZERO")
    monkeypatch.setattr(schwab_configurator["getpass"], "getpass", lambda _: next(answers))
    monkeypatch.setattr(
        sys,
        "argv",
        ["configure_schwab_runtime.py", "--secrets-file", str(secrets_path)],
    )

    assert schwab_configurator["main"]() == 0
    rendered = capsys.readouterr().out

    assert rendered.splitlines() == [
        "schwab_runtime: configured",
        "schwab_runtime: production_restart_required",
    ]
    assert client_id not in rendered
    assert client_secret not in rendered


def test_configuration_requires_zero_throttle_before_requesting_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secrets_path, _ = _runtime(tmp_path)
    original = secrets_path.read_bytes()
    credential_prompted = False

    def credential_prompt(_: str) -> str:
        nonlocal credential_prompted
        credential_prompted = True
        return "should-not-be-requested"

    monkeypatch.setattr("builtins.input", lambda _: "NO")
    monkeypatch.setattr(schwab_configurator["getpass"], "getpass", credential_prompt)
    monkeypatch.setattr(
        sys,
        "argv",
        ["configure_schwab_runtime.py", "--secrets-file", str(secrets_path)],
    )

    assert schwab_configurator["main"]() == 1
    assert capsys.readouterr().out.splitlines() == ["schwab_runtime: fail"]
    assert credential_prompted is False
    assert secrets_path.read_bytes() == original


def test_direct_configuration_rejects_missing_zero_throttle_verification(tmp_path: Path) -> None:
    secrets_path, _ = _runtime(tmp_path)
    original = secrets_path.read_bytes()
    client_id, client_secret = _client_values()

    with pytest.raises(ValueError, match="zero order throttle"):
        configure_schwab(
            secrets_path,
            client_id=client_id,
            client_secret=client_secret,
            order_throttle_verified=False,
        )

    assert secrets_path.read_bytes() == original
