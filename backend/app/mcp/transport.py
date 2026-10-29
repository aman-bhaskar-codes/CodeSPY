"""Bounded Unix pipe streams; the SDK retains all parsing and protocol handling."""

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.mcp.server import BoundedLineReader


class BoundedWriter:
    def __init__(self, wire: asyncio.StreamWriter, maximum: int = 262144):
        self.wire, self.maximum = wire, maximum

    async def write(self, data: str) -> None:
        if len(data.encode("utf-8")) > self.maximum:
            message = json.loads(data)
            data = (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "error": {"code": -32603, "message": "MCP response exceeds byte limit"},
                    }
                )
                + "\n"
            )
            if len(data.encode("utf-8")) > self.maximum:
                raise ValueError("MCP response identifier exceeds byte limit")
        self.wire.write(data.encode("utf-8"))

    async def flush(self) -> None:
        await self.wire.drain()


@asynccontextmanager
async def bounded_stdio() -> AsyncIterator[tuple[Any, Any]]:
    """Divert stray stdout and bound input before handing frames to the SDK.

    Async pipe reads are cancelled by closing their transport, without abandoning
    a thread blocked on a recycled stdin descriptor. Supported on macOS/Linux.
    """
    if sys.platform == "win32":
        raise RuntimeError("bounded MCP stdio currently requires a Unix pipe")
    saved_in, saved_out = os.dup(0), os.dup(1)
    wire = os.fdopen(os.dup(saved_out), "wb", buffering=0)
    pipe = os.fdopen(os.dup(saved_in), "rb", buffering=0)
    transport = None
    output_transport = None
    try:
        loop = asyncio.get_running_loop()
        output_protocol = asyncio.streams.FlowControlMixin(loop=loop)
        output_transport, _ = await loop.connect_write_pipe(lambda: output_protocol, wire)
        writer = asyncio.StreamWriter(output_transport, output_protocol, None, loop)
        reader = asyncio.StreamReader(limit=65536)
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, pipe)
        with open(os.devnull, "rb") as empty:  # noqa: ASYNC230 - fixed device, no user I/O
            os.dup2(empty.fileno(), 0)
        os.dup2(2, 1)
        yield BoundedLineReader(reader), BoundedWriter(writer)
    finally:
        if transport is not None:
            transport.close()
        else:
            pipe.close()
        os.dup2(saved_in, 0)
        os.dup2(saved_out, 1)
        os.close(saved_in)
        os.close(saved_out)
        if output_transport is not None:
            output_transport.close()
        else:
            wire.close()
