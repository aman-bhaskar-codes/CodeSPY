"""GitHub App OAuth adapter; credentials are explicit inputs, never discovered."""

import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from app.adapters.github.authlib_transport import AuthlibTransport
from app.adapters.github.transport import FixedGitHubTransport
from app.security.oauth import OAuthRejected, ProviderIdentity
from authlib.integrations.httpx_client import AsyncOAuth2Client  # type: ignore[import-untyped]
from authlib.oauth2.client import OAuth2Client  # type: ignore[import-untyped]


class GitHubOAuthProvider:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        callback: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        fixture: bool = False,
    ):
        parsed = urlsplit(callback)
        if (
            not client_id
            or not client_secret
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path != "/api/v1/auth/github/callback"
            or parsed.scheme not in {"http", "https"}
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"})
        ):
            raise ValueError("explicit App credentials and fixed callback required")
        if transport is not None and not (fixture and isinstance(transport, httpx.MockTransport)):
            raise ValueError("only an explicit fixture may override the pinned transport")
        self.client_id, self.client_secret, self.callback = client_id, client_secret, callback
        self.fixture_transport = transport

    def authorization_url(self, state: str, challenge: str) -> str:
        client = OAuth2Client(session=None, client_id=self.client_id, redirect_uri=self.callback)
        url, _ = client.create_authorization_url(
            "https://github.com/login/oauth/authorize",
            state=state,
            code_challenge=challenge,
            code_challenge_method="S256",
        )
        return str(url)

    async def exchange(self, code: str, verifier: str) -> ProviderIdentity:
        transport = self.fixture_transport or FixedGitHubTransport({"github.com", "api.github.com"})
        async with AsyncOAuth2Client(
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=self.callback,
            token_endpoint_auth_method="client_secret_post",  # noqa: S106 - OAuth method name, not a secret
            transport=AuthlibTransport(transport),
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(10, connect=5),
        ) as client:
            try:
                token: dict[str, Any] = await client.fetch_token(
                    "https://github.com/login/oauth/access_token",
                    code=code,
                    code_verifier=verifier,
                    grant_type="authorization_code",
                    headers={"Accept": "application/json"},
                )
                value = token.get("access_token")
                expires = token.get("expires_at")
                if (
                    not isinstance(value, str)
                    or not isinstance(expires, (float, int))
                    or expires <= time.time()
                ):
                    raise OAuthRejected("expiring provider grant required")
                # Explicit per-exchange access token, no email/login account linking.
                async with client.stream(
                    "GET",
                    "https://api.github.com/user",
                    headers={
                        "Authorization": "Bearer " + value,
                        "Accept": "application/vnd.github+json",
                    },
                ) as response:
                    if response.status_code != 200:
                        raise OAuthRejected("provider identity unavailable")
                    raw = bytearray()
                    async for block in response.aiter_bytes():
                        raw.extend(block)
                        if len(raw) > 65536:
                            raise OAuthRejected("provider identity exceeds bound")
                    import json

                    identity = json.loads(raw)
                subject = identity.get("id")
                if type(subject) is not int or subject <= 0:
                    raise OAuthRejected("numeric provider identity required")
                return ProviderIdentity(
                    subject,
                    str(identity.get("name") or "GitHub user")[:200],
                    value,
                    datetime.fromtimestamp(expires, UTC),
                )
            except OAuthRejected:
                raise
            except Exception:
                raise OAuthRejected("provider exchange unavailable") from None
