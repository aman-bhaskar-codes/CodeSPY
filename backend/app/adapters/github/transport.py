# ruff: noqa: ASYNC109
# Backend method signatures are defined by the locked httpcore interface.

"""Fixed origins with public DNS answers pinned at each TCP connection.

The locked httpcore backend keeps TLS verification and original-host SNI;
only its TCP destination is replaced with an already checked numeric address.
No environment proxy or second hostname resolution participates.
"""

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend

ALLOWED = frozenset({"github.com", "api.github.com", "codeload.github.com"})


def public_addresses(values: list[str]) -> list[str]:
    if not values or len(values) > 32:
        raise ValueError("DNS answer exceeds supported bounds")
    result = []
    for value in values:
        address = ipaddress.ip_address(value)
        if not address.is_global or address.is_multicast or address.is_unspecified:
            raise ValueError("nonpublic provider destination denied")
        if str(address) not in result:
            result.append(str(address))
    return result


class PinnedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, hosts: frozenset[str]):
        self.hosts = hosts
        self.delegate = AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if host not in self.hosts or port != 443 or local_address is not None:
            raise ValueError("provider TCP origin denied")
        async with asyncio.timeout(min(timeout or 5, 5)):
            answers = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
            addresses = public_addresses([str(answer[4][0]) for answer in answers])
            # Connect to the checked numeric destination. httpcore later starts
            # TLS with the original URL host, retaining certificate/SNI checks.
            return await self.delegate.connect_tcp(
                addresses[0], port, timeout=timeout, socket_options=socket_options
            )

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None
    ) -> httpcore.AsyncNetworkStream:
        raise ValueError("unix provider transport denied")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class CoreStream(httpx.AsyncByteStream):
    def __init__(self, stream: Any):
        self.stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for value in self.stream:
            yield value

    async def aclose(self) -> None:
        await self.stream.aclose()


class FixedGitHubTransport(httpx.AsyncBaseTransport):
    def __init__(self, hosts: set[str]):
        if not hosts or not hosts <= ALLOWED:
            raise ValueError("fixed GitHub origins required")
        self.hosts = frozenset(hosts)
        self.pool = httpcore.AsyncConnectionPool(
            network_backend=PinnedBackend(self.hosts),
            max_connections=4,
            max_keepalive_connections=2,
            retries=0,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if (
            url.scheme != "https"
            or url.host not in self.hosts
            or url.port not in {None, 443}
            or url.username
            or url.password
            or url.fragment
        ):
            raise ValueError("provider HTTP origin denied")
        response = await self.pool.handle_async_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=url.raw_scheme, host=url.raw_host, port=url.port, target=url.raw_path
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
        )
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=CoreStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self.pool.aclose()
