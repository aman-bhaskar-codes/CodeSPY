"""Provider input checks; validation alone does not qualify a live DNS transport."""

import hashlib
import hmac
from urllib.parse import urlsplit


def verify_webhook(body: bytes, signature: str, secret: bytes) -> str:
    if not secret or len(body) > 1048576:
        raise ValueError("invalid webhook envelope")
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("webhook signature differs")
    return hashlib.sha256(body).hexdigest()


def validate_archive_redirect(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "codeload.github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.fragment
    ):
        raise ValueError("archive redirect origin denied")
    return "codeload.github.com"
