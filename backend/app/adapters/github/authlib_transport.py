"""Explicit bridge for locked Authlib's httpx2 transport ABI.

Application HTTP remains httpx. Stream classes from these two distributions
are distinct; sharing them directly fails the client's runtime ABI checks.
"""

from types import TracebackType
from typing import Any

import httpx
import httpx2


class AuthlibResponseStream(httpx2.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream):
        self.stream = stream

    async def __aiter__(self) -> Any:
        size = 0
        async for block in self.stream:
            size += len(block)
            if size > 65536:
                raise ValueError("OAuth control response exceeds bound")
            yield block

    async def aclose(self) -> None:
        await self.stream.aclose()


class AuthlibTransport:
    def __init__(self, delegate: httpx.AsyncBaseTransport):
        self.delegate = delegate

    async def __aenter__(self) -> "AuthlibTransport":
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def handle_async_request(self, request: Any) -> Any:
        body = await request.aread()
        if len(body) > 65536:
            raise ValueError("OAuth control request exceeds bound")
        converted = httpx.Request(
            request.method,
            str(request.url),
            headers=request.headers.raw,
            content=body,
            extensions=request.extensions,
        )
        response = await self.delegate.handle_async_request(converted)
        assert isinstance(response.stream, httpx.AsyncByteStream)
        return httpx2.Response(
            response.status_code,
            headers=response.headers.raw,
            stream=AuthlibResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self.delegate.aclose()
