import httpx
import pytest
from app.adapters.github.http import ProviderFailure
from app.adapters.github.public import GitHubPublicProvider

SHA = "a" * 40


def client(handler):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
    )


async def test_default_branch_resolution_pin_and_credentialless_redirect():
    seen = []

    async def respond(request):
        seen.append(request)
        assert "authorization" not in request.headers and "cookie" not in request.headers
        path = request.url.path
        if path == "/repos/owner/repo":
            return httpx.Response(200, json={"private": False, "default_branch": "main"})
        if path == "/repos/owner/repo/commits/main":
            return httpx.Response(200, json={"sha": SHA})
        if path == f"/repos/owner/repo/zipball/{SHA}":
            return httpx.Response(
                302,
                headers={"Location": f"https://codeload.github.com/owner/repo/legacy.zip/{SHA}"},
            )
        assert request.url.host == "codeload.github.com"
        return httpx.Response(200, content=b"zip")

    async with client(respond) as http:
        provider = GitHubPublicProvider(http, fixture=True)
        commit = await provider.resolve("owner/repo", None)
        assert commit == SHA
        assert b"".join([c async for c in provider.archive("owner/repo", commit)]) == b"zip"
    assert len(seen) == 4


async def test_explicit_ref_skips_repository_lookup_and_quotes_ref():
    async def respond(request):
        assert request.url.raw_path.decode() == "/repos/owner/repo/commits/feature%2Fx"
        return httpx.Response(200, json={"sha": SHA})

    async with client(respond) as http:
        assert (
            await GitHubPublicProvider(http, fixture=True).resolve("owner/repo", "feature/x") == SHA
        )


@pytest.mark.parametrize(
    ("status", "headers", "code", "transient"),
    [
        (404, {}, "repository_unavailable", False),
        (403, {}, "repository_unavailable", False),
        (403, {"x-ratelimit-remaining": "0"}, "provider_rate_limited", True),
        (429, {"retry-after": "30"}, "provider_rate_limited", True),
        (503, {}, "provider_unavailable", True),
    ],
)
async def test_failure_classification(status, headers, code, transient):
    async def respond(_):
        return httpx.Response(status, headers=headers)

    async with client(respond) as http:
        with pytest.raises(ProviderFailure) as error:
            await GitHubPublicProvider(http, fixture=True).resolve("owner/repo", "main")
    assert (error.value.code, error.value.transient) == (code, transient)


async def test_private_or_malformed_repository_is_unavailable():
    async def respond(_):
        return httpx.Response(200, json={"private": True, "default_branch": "main"})

    async with client(respond) as http:
        with pytest.raises(ProviderFailure) as error:
            await GitHubPublicProvider(http, fixture=True).resolve("owner/repo", None)
    assert error.value.code == "repository_unavailable"


async def test_archive_rejects_foreign_redirect_and_oversize():
    async def foreign(_):
        return httpx.Response(302, headers={"Location": "https://evil.test/x.zip"})

    async with client(foreign) as http:
        with pytest.raises(ValueError, match="redirect origin denied"):
            _ = [c async for c in GitHubPublicProvider(http, fixture=True).archive("o/r", SHA)]

    async def big(request):
        if request.url.host == "api.github.com":
            return httpx.Response(
                302, headers={"Location": f"https://codeload.github.com/o/r/legacy.zip/{SHA}"}
            )
        return httpx.Response(200, content=b"x" * (104857600 + 1))

    async with client(big) as http:
        with pytest.raises(ProviderFailure) as error:
            _ = [c async for c in GitHubPublicProvider(http, fixture=True).archive("o/r", SHA)]
    assert error.value.code == "archive_limit_exceeded"


async def test_every_request_carries_a_user_agent_including_the_archive_hops():
    """GitHub answers 403 to requests without one; the archive requests are built by hand."""
    seen = []

    async def handler(request):
        seen.append((request.url.host, request.headers.get("user-agent")))
        if request.url.host == "api.github.com":
            return httpx.Response(
                302, headers={"Location": f"https://codeload.github.com/o/r/legacy.zip/{SHA}"}
            )
        return httpx.Response(200, content=b"zip")

    async with client(handler) as http:
        _ = [c async for c in GitHubPublicProvider(http, fixture=True).archive("o/r", SHA)]
    assert [host for host, _ in seen] == ["api.github.com", "codeload.github.com"]
    assert all(agent == "codespy-public-import" for _, agent in seen)


def test_rejects_unqualified_or_credentialed_clients():
    with pytest.raises(ValueError, match="fixed-origin"):
        GitHubPublicProvider(httpx.AsyncClient())
    with pytest.raises(ValueError, match="unauthenticated"):
        GitHubPublicProvider(
            httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(200)),
                headers={"Authorization": "Bearer x"},
            ),
            fixture=True,
        )
