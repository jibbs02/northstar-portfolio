from dataclasses import replace

import pytest

from northstar.config import Settings, validate_production_hostname
from northstar.operations.config import validate_schwab_environment


def test_environment_selects_public_hostname(monkeypatch):
    monkeypatch.setenv("NORTHSTAR_PRODUCTION_HOSTNAME", "portfolio.example.org")
    assert Settings.from_env().public_origin == "https://portfolio.example.org"


@pytest.mark.parametrize(
    "hostname",
    [
        "https://example.com",
        "example.com:443",
        "*.example.com",
        "example.com/path",
        "127.0.0.1",
        "example.com@attacker.org",
        "Example.com",
        "example.com.",
    ],
)
def test_public_hostname_rejects_ambiguous_origins(hostname):
    with pytest.raises(ValueError):
        validate_production_hostname(hostname)


def test_custom_host_keeps_exact_schwab_callback_boundary(settings):
    configured = replace(settings, production_hostname="portfolio.example.org")
    configured.validate()
    values = {
        "NORTHSTAR_PRODUCTION_HOSTNAME": configured.production_hostname,
        "NORTHSTAR_SCHWAB_ENABLED": "1",
        "NORTHSTAR_SCHWAB_ORDER_THROTTLE_VERIFIED": "1",
        "SCHWAB_CLIENT_ID": "synthetic-client",
        "SCHWAB_CLIENT_SECRET": "synthetic-secret",
        "SCHWAB_REDIRECT_URI": configured.public_origin + "/integrations/schwab/callback",
    }
    validate_schwab_environment(values)
    values["SCHWAB_REDIRECT_URI"] = "https://attacker.example.org/integrations/schwab/callback"
    with pytest.raises(ValueError):
        validate_schwab_environment(values)
