"""Unauthenticated public-repository resolve and archive over fixed-origin transport."""

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any
from urllib.parse import quote

import httpx
from app.adapters.github.http import ProviderFailure
from app.adapters.github.transport import FixedGitHubTransport
from app.domain.errors import ProviderCapacityUnsupported
from app.security.github import validate_archive_redirect

MAX_ARCHIVE_BYTES = 104857600
_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}")
_SHA = re.compile(r"[0-9a-f]{40}")
_USER_AGENT = "codespy-public-import"  # GitHub rejects requests that carry no User-Agent
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": _USER_AGENT,
}


class GitHubPublicProvider:
    """Reads only public data; carries no credentials, cookies or redirects."""

    def __init__(self, client: httpx.AsyncClient, *, fixture: bool = False):
        transport = client._transport
        if client.follow_redirects or (
            not isinstance(transport, FixedGitHubTransport)
            and not (fixture and isinstance(transport, httpx.MockTransport))
        ):
            raise ValueError("qualified fixed-origin transport required")
        if client.auth is not None or "authorization" in client.headers or client.cookies:
            raise ValueError("unauthenticated client required")
        self.client = client

    @staticmethod
    def status(response: httpx.Response) -> None:
        code = response.status_code
        limited = response.headers.get("retry-after") is not None or (
            response.headers.get("x-ratelimit-remaining") == "0"
        )
        if code == 429 or (code == 403 and limited):
            raise ProviderFailure("provider_rate_limited", transient=True)
        if code in {401, 403, 404, 451}:
            raise ProviderFailure("repository_unavailable", transient=False)
        if code >= 500:
            raise ProviderFailure("provider_unavailable", transient=True)
        if code != 200:
            raise ProviderFailure("provider_unavailable", transient=False)

    async def _json(self, path: str) -> dict[str, Any]:
        async with asyncio.timeout(30):
            async with self.client.stream(
                "GET",
                "https://api.github.com" + path,
                headers=_HEADERS,
                timeout=httpx.Timeout(10, connect=5),
            ) as response:
                self.status(response)
                raw = bytearray()
                async for block in response.aiter_bytes():
                    raw.extend(block)
                    if len(raw) > 2097152:
                        raise ProviderCapacityUnsupported("provider page exceeds bound")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ProviderFailure("provider_unavailable", transient=False)
                return value

    @staticmethod
    def _name(full_name: str) -> str:
        if not _NAME.fullmatch(full_name):
            raise ValueError("invalid repository name")
        return full_name

    async def resolve(self, full_name: str, ref: str | None) -> str:
        """Return the full 40-hex commit for a ref (default branch when omitted)."""
        name = self._name(full_name)
        if ref is None:
            repository = await self._json("/repos/" + name)
            branch = repository.get("default_branch")
            if repository.get("private") is not False or not isinstance(branch, str):
                raise ProviderFailure("repository_unavailable", transient=False)
            ref = branch
        if len(ref.encode()) > 255:
            raise ValueError("ref exceeds bound")
        value = await self._json("/repos/" + name + "/commits/" + quote(ref, safe=""))
        commit = value.get("sha")
        if not isinstance(commit, str) or not _SHA.fullmatch(commit):
            raise ProviderFailure("provider_unavailable", transient=False)
        return commit

    async def archive(self, full_name: str, commit: str) -> AsyncIterator[bytes]:
        if not _SHA.fullmatch(commit):
            raise ValueError("full pinned commit required")
        url = "https://api.github.com/repos/" + self._name(full_name) + "/zipball/" + commit
        headers = dict(_HEADERS)
        async with asyncio.timeout(120):
            for _ in range(3):
                request = httpx.Request(
                    "GET",
                    url,
                    headers=headers,
                    extensions={"timeout": {"connect": 5, "read": 10, "write": 10, "pool": 5}},
                )
                response = await self.client.send(
                    request, stream=True, auth=None, follow_redirects=False
                )
                async with aclosing(response):
                    if response.status_code in {301, 302, 303, 307, 308}:
                        url = response.headers.get("location", "")
                        validate_archive_redirect(url)
                        headers = {"Accept": "application/zip", "User-Agent": _USER_AGENT}
                        continue
                    self.status(response)
                    size = 0
                    async for block in response.aiter_bytes(65536):
                        size += len(block)
                        if size > MAX_ARCHIVE_BYTES:
                            raise ProviderFailure("archive_limit_exceeded", transient=False)
                        yield block
                    return
            raise ProviderFailure("provider_unavailable", transient=False)
