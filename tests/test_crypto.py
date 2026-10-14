from __future__ import annotations

import os

import pytest

from northstar.crypto import CryptoError, Vault


def test_vault_round_trip_and_blind_index_are_deterministic() -> None:
    vault = Vault(os.urandom(32), os.urandom(32), 1)
    encrypted = vault.encrypt_json({"balance": 123.45, "account": "synthetic"}, associated_data=b"record:1")

    assert b"synthetic" not in encrypted
    assert vault.decrypt_json(encrypted, associated_data=b"record:1")["balance"] == 123.45
    assert vault.blind_index("account", "abc") == vault.blind_index("account", "abc")
    assert vault.blind_index("account", "abc") != vault.blind_index("account", "def")


def test_vault_rejects_tampering_and_wrong_context() -> None:
    vault = Vault(os.urandom(32), os.urandom(32), 1)
    encrypted = bytearray(vault.encrypt(b"private", associated_data=b"correct"))
    encrypted[-1] ^= 1

    with pytest.raises(CryptoError):
        vault.decrypt(bytes(encrypted), associated_data=b"correct")
    with pytest.raises(CryptoError):
        vault.decrypt(vault.encrypt(b"private", associated_data=b"correct"), associated_data=b"wrong")
