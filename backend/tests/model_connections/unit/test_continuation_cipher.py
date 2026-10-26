"""Synthetic-key authenticated encryption; no owner secrets."""

import pytest
from app.services.model_continuations import ContinuationCipher
from cryptography.exceptions import InvalidTag


def test_cipher_is_context_bound_and_non_deterministic():
    cipher = ContinuationCipher(bytes(range(32)))
    first = cipher.encrypt(b"opaque-signature", "scope-one")
    second = cipher.encrypt(b"opaque-signature", "scope-one")
    assert first != second
    assert b"opaque-signature" not in first
    assert cipher.decrypt(first, "scope-one") == b"opaque-signature"
    with pytest.raises(InvalidTag):
        cipher.decrypt(first, "scope-two")
    with pytest.raises(InvalidTag):
        cipher.decrypt(first[:-1] + bytes([first[-1] ^ 1]), "scope-one")
