"""Bounded user/App intersection and archive download over fixed-origin transport."""

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx
from app.adapters.github.transport import FixedGitHubTransport
from app.domain.errors import PersistenceError, ProviderCapacityUnsupported, ScopeViolation
from app.security.github import validate_archive_redirect
from app.services.github_acquisition import RepositoryAccess


class ProviderFailure(PersistenceError):  # noqa: N818 - provider failure contract
    def __init__(self, code: str, *, transient: bool):
        self.code, self.transient = code, transient
        super().__init__(code)


class GitHubHTTPProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        user_token: Callable[[UUID], Awaitable[str]],
        installation_token: Callable[[int, int], Awaitable[tuple[str, datetime]]],
        *,
        fixture: bool = False,
    ):
        transport = client._transport
        if client.follow_redirects or (
            not isinstance(transport, FixedGitHubTransport)
            and not (fixture and isinstance(transport, httpx.MockTransport))
        ):
            raise ValueError("qualified fixed-origin transport required")
        # No client-level credentials/default Authorization may leak cross-origin.
        if client.auth is not None or "authorization" in client.headers or client.cookies:
            raise ValueError("request-scoped provider authorization required")
        self.client, self.user_token, self.installation_token = (
            client,
            user_token,
            installation_token,
        )
        self.names: dict[tuple[int, int], str] = {}

    @staticmethod
    def headers(token: str) -> dict[str, str]:
        if not token or len(token) > 16384 or any(ord(c) < 32 for c in token):
            raise ScopeViolation("provider token unavailable")
        return {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @staticmethod
    def status(response: httpx.Response) -> None:
        if response.status_code in {401, 403, 404}:
            raise ProviderFailure("access_revoked", transient=False)
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderFailure("provider_unavailable", transient=True)
        if response.status_code != 200:
            raise ProviderFailure("provider_unavailable", transient=False)

    async def json(self, path: str, token: str) -> dict[str, Any]:
        async with asyncio.timeout(30):
            async with self.client.stream(
                "GET",
                "https://api.github.com" + path,
                headers=self.headers(token),
                timeout=httpx.Timeout(10, connect=5),
            ) as response:
                self.status(response)
                raw = bytearray()
                async for block in response.aiter_bytes():
                    raw.extend(block)
                    if len(raw) > 2097152:
                        raise ProviderCapacityUnsupported("provider page exceeds bound")
                import json

                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ProviderFailure("provider_unavailable", transient=False)
                return value

    async def pages(self, path: str, field: str, token: str) -> AsyncIterator[dict[str, Any]]:
        seen = 0
        for page in range(1, 51):
            value = await self.json(f"{path}?per_page=100&page={page}", token)
            total, items = value.get("total_count"), value.get(field)
            if (
                type(total) is not int
                or total < 0
                or not isinstance(items, list)
                or len(items) > 100
            ):
                raise ProviderFailure("provider_unavailable", transient=False)
            if total > 5000:
                raise ProviderCapacityUnsupported("provider repository capacity unsupported")
            for item in items:
                if not isinstance(item, dict):
                    raise ProviderFailure("provider_unavailable", transient=False)
                seen += 1
                if seen > 5000:
                    raise ProviderCapacityUnsupported("provider repository capacity unsupported")
                yield item
            if seen >= total:
                return
            if not items:
                raise ProviderFailure("provider_unavailable", transient=True)
        raise ProviderCapacityUnsupported("provider pagination exceeds bound")

    async def verify_access(
        self, principal: UUID, repository_id: int, installation_id: int
    ) -> RepositoryAccess:
        token = await self.user_token(principal)
        found = False
        async for installation in self.pages("/user/installations", "installations", token):
            if installation.get("id") == installation_id:
                found = True
        if not found:
            raise ScopeViolation("installation not accessible to user")
        selected = None
        async for repository in self.pages(
            f"/user/installations/{installation_id}/repositories", "repositories", token
        ):
            if repository.get("id") == repository_id:
                selected = repository
        if selected is None:
            raise ScopeViolation("repository not accessible to user and App")
        name = selected.get("full_name")
        account = selected.get("owner", {}).get("id")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", name)
            or type(account) is not int
            or account <= 0
        ):
            raise ProviderFailure("provider_unavailable", transient=False)
        if len(self.names) >= 5000 and (repository_id, installation_id) not in self.names:
            raise ProviderCapacityUnsupported("provider identity cache exceeds bound")
        self.names[(repository_id, installation_id)] = name
        return RepositoryAccess(repository_id, installation_id, account)

    async def token(self, access: RepositoryAccess) -> str:
        token, expires = await self.installation_token(access.repository_id, access.installation_id)
        if expires.tzinfo is None or expires <= datetime.now(UTC):
            raise ProviderFailure("provider_unavailable", transient=True)
        return token

    def name(self, access: RepositoryAccess) -> str:
        value = self.names.get((access.repository_id, access.installation_id))
        if value is None:
            raise ScopeViolation("provider intersection must be checked first")
        return value

    async def resolve(self, access: RepositoryAccess, ref: str) -> str:
        if len(ref.encode()) > 255:
            raise ValueError("ref exceeds bound")
        value = await self.json(
            "/repos/" + self.name(access) + "/commits/" + quote(ref, safe=""),
            await self.token(access),
        )
        commit = value.get("sha")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ProviderFailure("provider_unavailable", transient=False)
        return commit

    async def archive(self, access: RepositoryAccess, commit: str) -> AsyncIterator[bytes]:
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("full pinned commit required")
        url = "https://api.github.com/repos/" + self.name(access) + "/zipball/" + commit
        headers = self.headers(await self.token(access))
        async with asyncio.timeout(120):
            for _ in range(3):
                request = httpx.Request(
                    "GET",
                    url,
                    headers=headers,
                    extensions={"timeout": {"connect": 5, "read": 10, "write": 10, "pool": 5}},
                )
                # Construct independently of the client's cookie jar/default headers.
                response = await self.client.send(
                    request, stream=True, auth=None, follow_redirects=False
                )
                async with aclosing(response):
                    if response.status_code in {301, 302, 303, 307, 308}:
                        url = response.headers.get("location", "")
                        validate_archive_redirect(url)
                        # Do not forward any credentials or provider cookies.
                        headers = {"Accept": "application/zip"}
                        continue
                    self.status(response)
                    size = 0
                    async for block in response.aiter_bytes(65536):
                        size += len(block)
                        if size > 104857600:
                            raise ProviderFailure("archive_limit_exceeded", transient=False)
                        yield block
                    return
            raise ProviderFailure("provider_unavailable", transient=False)
