"""Authenticated opaque keysets bind every page to its authorized resource."""

import base64
import hashlib
import hmac
import json
from typing import Any

from app.domain.canonical_json import canonical_bytes


class CursorCodec:
    def __init__(self, secret: bytes):
        if len(secret) < 32:
            raise ValueError("cursor signing secret must have at least32 bytes")
        self.secret = secret

    def encode(self, scope: dict[str, Any], position: str) -> str:
        raw = canonical_bytes({"scope": scope, "position": position})
        signature = hmac.digest(self.secret, raw, hashlib.sha256)
        return base64.urlsafe_b64encode(raw + signature).rstrip(b"=").decode()

    def decode(self, token: str, scope: dict[str, Any]) -> str:
        try:
            if len(token) > 4096:
                raise ValueError("cursor exceeds bound")
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            body, supplied = raw[:-32], raw[-32:]
            if not hmac.compare_digest(hmac.digest(self.secret, body, hashlib.sha256), supplied):
                raise ValueError("cursor signature differs")
            value = json.loads(body)
            if value["scope"] != scope or not isinstance(value["position"], str):
                raise ValueError("cursor scope differs")
            if self.encode(scope, value["position"]) != token:
                raise ValueError("noncanonical cursor")
            return str(value["position"])
        except (KeyError, TypeError, UnicodeError, ValueError):
            raise ValueError("invalid cursor") from None
