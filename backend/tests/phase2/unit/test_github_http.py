from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from app.adapters.github.http import GitHubHTTPProvider


async def test_user_app_intersection_sha_and_cross_origin_token_stripping():
    calls = []

    async def respond(request):
        calls.append(request)
        path = request.url.path
        if path == "/user/installations":
            return httpx.Response(200, json={"total_count": 1, "installations": [{"id": 22}]})
        if path == "/user/installations/22/repositories":
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "repositories": [{"id": 11, "full_name": "owner/repo", "owner": {"id": 33}}],
                },
            )
        if path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": "a" * 40})
        if path.endswith("/zipball/" + "a" * 40):
            assert request.headers["authorization"] == "Bearer synthetic-installation"
            return httpx.Response(
                302,
                headers={
                    "Set-Cookie": "provider=synthetic; Domain=.github.com; Path=/; Secure",
                    "Location": "https://codeload.github.com/owner/repo/legacy.zip/" + "a" * 40,
                },
            )
        assert request.url.host == "codeload.github.com"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, content=b"fixture-zip")

    async def user(_):
        return "synthetic-user"

    async def installation(_repo, _install):
        return ("synthetic-installation", datetime.now(UTC) + timedelta(minutes=5))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), trust_env=False, follow_redirects=False
    ) as client:
        provider = GitHubHTTPProvider(client, user, installation, fixture=True)
        access = await provider.verify_access(uuid4(), 11, 22)
        assert access.account_id == 33
        assert await provider.resolve(access, "main") == "a" * 40
        assert (
            b"".join([part async for part in provider.archive(access, "a" * 40)]) == b"fixture-zip"
        )
    assert len(calls) == 5
