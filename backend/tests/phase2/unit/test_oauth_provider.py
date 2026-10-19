from urllib.parse import parse_qs

import httpx
from app.adapters.github.oauth import GitHubOAuthProvider


async def test_oauth_exchange_is_post_pkce_fixed_callback_and_numeric_identity():
    async def respond(request):
        if request.url.host == "github.com":
            assert request.method == "POST" and not request.url.query
            form = parse_qs(request.content.decode())
            assert form["code_verifier"] == ["v" * 43]
            assert form["redirect_uri"] == ["http://localhost:8000/api/v1/auth/github/callback"]
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-grant",
                    "token_type": "bearer",
                    "expires_in": 3600,
                },
            )
        assert request.url == "https://api.github.com/user"
        return httpx.Response(200, json={"id": 42, "name": "Fixture"})

    provider = GitHubOAuthProvider(
        "fixture-client",
        "fixture-secret",
        "http://localhost:8000/api/v1/auth/github/callback",
        transport=httpx.MockTransport(respond),
        fixture=True,
    )
    url = provider.authorization_url("state", "challenge")
    assert "code_challenge_method=S256" in url and "scope=repo" not in url
    identity = await provider.exchange("synthetic-code", "v" * 43)
    assert identity.subject == 42 and identity.token == "synthetic-grant"  # noqa: S105 - synthetic fixture only
