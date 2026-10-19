import hashlib
import hmac

import pytest
from app.security.github import validate_archive_redirect, verify_webhook


def test_webhook_authenticates_raw_bounded_bytes_before_parsing():
    body = b'{"action":"deleted"}'
    signature = "sha256=" + hmac.new(b"fixture-only-secret", body, hashlib.sha256).hexdigest()
    assert (
        verify_webhook(body, signature, b"fixture-only-secret") == hashlib.sha256(body).hexdigest()
    )
    for value in [body + b" ", b"x" * 1048577]:
        with pytest.raises(ValueError):
            verify_webhook(value, signature, b"fixture-only-secret")


def test_archive_redirect_is_fixed_origin_and_strips_auth():
    assert (
        validate_archive_redirect("https://codeload.github.com/owner/repo/legacy.zip/" + "a" * 40)
        == "codeload.github.com"
    )
    for url in [
        "http://codeload.github.com/a",
        "https://127.0.0.1/a",
        "https://codeload.github.com:444/a",
        "https://user@codeload.github.com/a",
        "https://evil.test/a",
    ]:
        with pytest.raises(ValueError):
            validate_archive_redirect(url)
