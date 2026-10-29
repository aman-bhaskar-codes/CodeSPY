"""Exercise frame bounds on the executable transport, without database access."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.mark.parametrize("suffix", [b"\n", b""])
def test_real_stdio_rejects_oversized_frame_before_parsing(suffix):
    env = {"PATH": os.defpath, "PYTHONPATH": str(Path(__file__).parents[3])}
    env.update(
        {
            "CODESPY_MCP_ORG_ID": str(uuid4()),
            "CODESPY_MCP_PRINCIPAL_ID": str(uuid4()),
            "CODESPY_MCP_REPOSITORY_ALLOWLIST": str(uuid4()),
        }
    )
    proc = subprocess.run(
        [sys.executable, "-m", "app.mcp"],
        cwd=Path(__file__).parents[3],
        env=env,
        input=b"x" * 70000 + suffix,
        capture_output=True,
        timeout=8,
    )
    assert b"Incoming frame exceeds" in proc.stderr
    assert b"x" * 100 not in proc.stderr


async def test_modern_discovery_and_schemas_without_initialization():
    env = {"PATH": os.defpath, "PYTHONPATH": str(Path(__file__).parents[3])}
    env.update(
        {
            "CODESPY_MCP_ORG_ID": str(uuid4()),
            "CODESPY_MCP_PRINCIPAL_ID": str(uuid4()),
            "CODESPY_MCP_REPOSITORY_ALLOWLIST": str(uuid4()),
        }
    )
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "qualification", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    frames = [
        {"jsonrpc": "2.0", "id": i, "method": method, "params": {"_meta": meta}}
        for i, method in enumerate(("server/discover", "tools/list", "PRIVATE_PROTOCOL_CANARY"), 1)
    ]
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.mcp",
        cwd=Path(__file__).parents[3],
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    results = {}
    assert proc.stdin is not None and proc.stdout is not None
    try:
        for frame in frames:
            proc.stdin.write((json.dumps(frame) + "\n").encode())
            await proc.stdin.drain()
            line = await asyncio.wait_for(proc.stdout.readline(), 5)
            item = json.loads(line)
            results[item["id"]] = item
    finally:
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), 5)
    assert proc.returncode == 0
    assert proc.stderr is not None
    assert b"PRIVATE_PROTOCOL_CANARY" not in await proc.stderr.read()
    assert results[3]["error"]["code"] == -32601
    assert "2026-07-28" in results[1]["result"]["supportedVersions"]
    tools = results[2]["result"]["tools"]
    assert len(tools) == 13
    assert all(tool["outputSchema"]["type"] == "object" for tool in tools)
    assert all(tool["annotations"]["readOnlyHint"] for tool in tools)
