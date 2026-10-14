from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from northstar.crypto import Vault
from northstar.crypto.vault import decode_key

_DEMO_DATA_KEY = "ZGVtby1kYXRhLWtleS0wMDAwMDAwMDAwMDAwMDAwMDA="
_DEMO_DATABASE_KEY = "ZGVtby1kYXRhYmFzZS1rZXkwMDAwMDAwMDAwMDAwMDA="
_DEMO_BACKUP_KEY = "ZGVtby1iYWNrdXAta2V5LTAwMDAwMDAwMDAwMDAwMDA="
_DEMO_INDEX_KEY = "ZGVtby1pbmRleC1rZXktMDAwMDAwMDAwMDAwMDAwMDA="
PRODUCTION_HOSTNAME = "northstar.example.com"
_CLOUDFLARE_TEAM = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_EMAIL = re.compile(r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])$")


def validate_production_hostname(value: str) -> str:
    """Accept a DNS hostname only, never a URL, port, or wildcard."""
    labels = value.split(".")
    if (
        len(value) > 253
        or len(labels) < 2
        or any(_CLOUDFLARE_TEAM.fullmatch(label) is None for label in labels)
        or labels[-1].isdigit()
    ):
        raise ValueError("Production hostname must be a lowercase DNS hostname")
    return value


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    environment: str
    auth_mode: str
    database_path: Path
    backup_dir: Path
    secrets_path: Path | None
    database_key: bytes
    data_key: bytes
    backup_key: bytes
    blind_index_key: bytes
    session_key: str
    key_version: int
    backup_daily_retention: int
    backup_monthly_retention: int
    cf_issuer: str | None
    cf_audience: str | None
    allowed_email: str | None
    secure_cookies: bool
    production_hostname: str = PRODUCTION_HOSTNAME
    schwab_enabled: bool = False
    schwab_client_id: str | None = field(default=None, repr=False)
    schwab_client_secret: str | None = field(default=None, repr=False)
    schwab_redirect_uri: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        environment = os.getenv("NORTHSTAR_ENV", "development").strip().lower()
        auth_mode = os.getenv("NORTHSTAR_AUTH_MODE", "local").strip().lower()
        is_nonproduction = environment in {"development", "demo", "test"}

        database_value = os.getenv("NORTHSTAR_DATABASE_KEY") or (
            _DEMO_DATABASE_KEY if is_nonproduction else ""
        )
        data_value = os.getenv("NORTHSTAR_DATA_KEY") or (_DEMO_DATA_KEY if is_nonproduction else "")
        backup_value = os.getenv("NORTHSTAR_BACKUP_KEY") or (_DEMO_BACKUP_KEY if is_nonproduction else "")
        index_value = os.getenv("NORTHSTAR_BLIND_INDEX_KEY") or (_DEMO_INDEX_KEY if is_nonproduction else "")
        session_key = os.getenv("NORTHSTAR_SESSION_KEY") or (
            "demo-session-key-not-for-production-000000000000" if is_nonproduction else ""
        )

        settings = cls(
            environment=environment,
            auth_mode=auth_mode,
            database_path=Path(os.getenv("NORTHSTAR_DATABASE_PATH", "./runtime/northstar.db")).expanduser(),
            backup_dir=Path(os.getenv("NORTHSTAR_BACKUP_DIR", "./runtime/backups")).expanduser(),
            secrets_path=(
                Path(value).expanduser() if (value := os.getenv("NORTHSTAR_SECRETS_PATH")) else None
            ),
            database_key=decode_key(database_value, "NORTHSTAR_DATABASE_KEY"),
            data_key=decode_key(data_value, "NORTHSTAR_DATA_KEY"),
            backup_key=decode_key(backup_value, "NORTHSTAR_BACKUP_KEY"),
            blind_index_key=decode_key(index_value, "NORTHSTAR_BLIND_INDEX_KEY"),
            session_key=session_key,
            key_version=int(os.getenv("NORTHSTAR_KEY_VERSION", "1")),
            backup_daily_retention=int(os.getenv("NORTHSTAR_BACKUP_DAILY_RETENTION", "14")),
            backup_monthly_retention=int(os.getenv("NORTHSTAR_BACKUP_MONTHLY_RETENTION", "12")),
            cf_issuer=os.getenv("NORTHSTAR_CF_ISSUER") or None,
            cf_audience=os.getenv("NORTHSTAR_CF_AUDIENCE") or None,
            allowed_email=(os.getenv("NORTHSTAR_ALLOWED_EMAIL") or "").strip().lower() or None,
            secure_cookies=_as_bool(os.getenv("NORTHSTAR_SECURE_COOKIES"), not is_nonproduction),
            production_hostname=os.getenv("NORTHSTAR_PRODUCTION_HOSTNAME", PRODUCTION_HOSTNAME).strip(),
            schwab_enabled=_as_bool(os.getenv("NORTHSTAR_SCHWAB_ENABLED"), False),
            schwab_client_id=(os.getenv("SCHWAB_CLIENT_ID") or "").strip() or None,
            schwab_client_secret=os.getenv("SCHWAB_CLIENT_SECRET") or None,
            schwab_redirect_uri=(os.getenv("SCHWAB_REDIRECT_URI") or "").strip() or None,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.environment not in {"development", "demo", "test", "production"}:
            raise ValueError("NORTHSTAR_ENV must be development, demo, test, or production")
        if self.auth_mode not in {"local", "cloudflare"}:
            raise ValueError("NORTHSTAR_AUTH_MODE must be local or cloudflare")
        if len(self.session_key) < 32:
            raise ValueError("NORTHSTAR_SESSION_KEY must be at least 32 characters")
        if self.backup_daily_retention < 0 or self.backup_monthly_retention < 0:
            raise ValueError("Backup retention values cannot be negative")
        if self.backup_daily_retention == 0 and self.backup_monthly_retention == 0:
            raise ValueError("At least one backup retention window must be enabled")
        if self.environment == "production" and self.auth_mode != "cloudflare":
            raise ValueError("Production requires Cloudflare Access authentication")
        if self.auth_mode == "cloudflare":
            validate_cloudflare_issuer(self.cf_issuer)
            validate_cloudflare_audience(self.cf_audience)
            normalize_allowed_email(self.allowed_email)
        if self.environment == "production" and not self.secure_cookies:
            raise ValueError("Production requires secure cookies")
        validate_production_hostname(self.production_hostname)
        if self.schwab_enabled:
            validate_schwab_client_value(self.schwab_client_id, "client ID", 512)
            validate_schwab_client_value(self.schwab_client_secret, "client secret", 2_048)
            validate_schwab_redirect_uri(
                self.schwab_redirect_uri,
                environment=self.environment,
                production_origin=self.public_origin,
            )

    @property
    def public_origin(self) -> str:
        return f"https://{self.production_hostname}"

    def validate_runtime_permissions(self) -> None:
        if self.environment != "production":
            return
        if self.secrets_path is None:
            raise ValueError("Production requires NORTHSTAR_SECRETS_PATH for permission validation")
        _require_private_file(self.secrets_path, "Secrets file")
        _require_private_directory(self.database_path.parent, "Database directory")
        _require_private_directory(self.backup_dir, "Backup directory")
        if self.database_path.exists() or self.database_path.is_symlink():
            _require_private_file(self.database_path, "Database file")
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.database_path}{suffix}")
            if sidecar.exists() or sidecar.is_symlink():
                _require_private_file(sidecar, f"Database {suffix[1:].upper()} file")

    def data_vault(self) -> Vault:
        return Vault(self.data_key, self.blind_index_key, self.key_version)

    def backup_vault(self) -> Vault:
        return Vault(self.backup_key, self.blind_index_key, self.key_version)


def _require_private_file(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"{label} does not exist: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"{label} must be a regular file and not a symlink: {path}")
    if metadata.st_mode & 0o077:
        raise ValueError(f"{label} must not be accessible by group or other users: {path}")
    if not metadata.st_mode & stat.S_IRUSR:
        raise ValueError(f"{label} must be readable by its owner: {path}")


def _require_private_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"{label} does not exist: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"{label} must be a directory and not a symlink: {path}")
    if metadata.st_mode & 0o077:
        raise ValueError(f"{label} must not be accessible by group or other users: {path}")


def validate_cloudflare_issuer(value: str | None) -> str:
    if value is None:
        raise ValueError("Cloudflare mode requires an issuer")
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    labels = hostname.split(".")
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or len(labels) != 3
        or labels[1:] != ["cloudflareaccess", "com"]
        or _CLOUDFLARE_TEAM.fullmatch(labels[0]) is None
        or value != f"https://{hostname}"
    ):
        raise ValueError("Cloudflare issuer must be the exact team issuer")
    return value


def validate_cloudflare_audience(value: str | None) -> str:
    if value is None or not value or len(value) > 512 or any(character.isspace() for character in value):
        raise ValueError("Cloudflare audience is malformed")
    return value


def normalize_allowed_email(value: str | None) -> str:
    normalized = (value or "").strip().casefold()
    if (
        not normalized
        or len(normalized) > 254
        or not normalized.isascii()
        or _EMAIL.fullmatch(normalized) is None
    ):
        raise ValueError("Cloudflare allowed email is malformed")
    return normalized


def validate_schwab_client_value(value: str | None, label: str, maximum_length: int) -> str:
    if (
        value is None
        or not value
        or len(value) > maximum_length
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(f"Schwab {label} is malformed")
    return value


def validate_schwab_redirect_uri(
    value: str | None, *, environment: str, production_origin: str
) -> str:
    if value is None:
        raise ValueError("Schwab redirect URI is required")
    parsed = urlsplit(value)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/integrations/schwab/callback"
    ):
        raise ValueError("Schwab redirect URI is malformed")
    if environment == "production":
        expected = f"{production_origin}/integrations/schwab/callback"
        if value != expected:
            raise ValueError("Production Schwab redirect URI must match the fixed callback")
    elif parsed.scheme == "https":
        if parsed.hostname is None:
            raise ValueError("Schwab redirect URI is malformed")
    elif parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("Nonproduction Schwab redirect URI must use HTTPS or loopback HTTP")
    return value
