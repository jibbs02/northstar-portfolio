from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"NSV1"
NONCE_BYTES = 12


class CryptoError(ValueError):
    """Raised when encrypted Northstar data cannot be authenticated."""


def decode_key(value: str, label: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError(f"{label} must be URL-safe base64") from exc
    if len(key) != 32:
        raise ValueError(f"{label} must decode to exactly 32 bytes")
    return key


@dataclass(frozen=True)
class Vault:
    encryption_key: bytes
    blind_index_key: bytes
    key_version: int = 1

    def __post_init__(self) -> None:
        if len(self.encryption_key) != 32 or len(self.blind_index_key) != 32:
            raise ValueError("Vault keys must be exactly 32 bytes")
        if self.key_version < 1 or self.key_version > 255:
            raise ValueError("Key version must be between 1 and 255")

    def encrypt(self, plaintext: bytes, *, associated_data: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(self.encryption_key).encrypt(nonce, plaintext, associated_data)
        return MAGIC + bytes([self.key_version]) + nonce + ciphertext

    def decrypt(self, envelope: bytes, *, associated_data: bytes) -> bytes:
        if len(envelope) < len(MAGIC) + 1 + NONCE_BYTES + 16 or not envelope.startswith(MAGIC):
            raise CryptoError("Invalid encrypted envelope")
        version = envelope[len(MAGIC)]
        if version != self.key_version:
            raise CryptoError("Encrypted data uses an unavailable key version")
        nonce_start = len(MAGIC) + 1
        nonce = envelope[nonce_start : nonce_start + NONCE_BYTES]
        ciphertext = envelope[nonce_start + NONCE_BYTES :]
        try:
            return AESGCM(self.encryption_key).decrypt(nonce, ciphertext, associated_data)
        except InvalidTag as exc:
            raise CryptoError("Encrypted data failed authentication") from exc

    def encrypt_json(self, value: dict[str, Any], *, associated_data: bytes) -> bytes:
        payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return self.encrypt(payload, associated_data=associated_data)

    def decrypt_json(self, envelope: bytes, *, associated_data: bytes) -> dict[str, Any]:
        decoded = json.loads(self.decrypt(envelope, associated_data=associated_data))
        if not isinstance(decoded, dict):
            raise CryptoError("Encrypted JSON payload is not an object")
        return decoded

    def blind_index(self, namespace: str, value: str) -> str:
        normalized = f"{namespace.strip().lower()}\x00{value.strip()}".encode()
        return hmac.new(self.blind_index_key, normalized, hashlib.sha256).hexdigest()
