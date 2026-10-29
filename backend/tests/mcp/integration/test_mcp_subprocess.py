"""Subprocess integration tests for CodeSPY MCP server entrypoint (python -m app.mcp).

Verifies safe stdio composition:
- Fail-closed configuration checks
- Stdout reservation strictly for protocol framing
- Clean EOF / shutdown lifecycle
- Allowlist and scope enforcement over real subprocess pipes
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from uuid import uuid4

PYTHON_EXE = sys.executable
BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def test_subprocess_fails_closed_missing_org() -> None:
    env = os.environ.copy()
    env.pop("CODESPY_MCP_ORG_ID", None)
    proc = subprocess.run(  # noqa: S603 - trusted python binary and arguments
        [PYTHON_EXE, "-m", "app.mcp"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "CODESPY_MCP_ORG_ID environment variable is required" in proc.stderr
    assert proc.stdout == ""


def test_subprocess_fails_closed_missing_principal() -> None:
    env = os.environ.copy()
    env["CODESPY_MCP_ORG_ID"] = str(uuid4())
    env.pop("CODESPY_MCP_PRINCIPAL_ID", None)
    proc = subprocess.run(  # noqa: S603 - trusted python binary and arguments
        [PYTHON_EXE, "-m", "app.mcp"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "CODESPY_MCP_PRINCIPAL_ID environment variable is required" in proc.stderr
    assert proc.stdout == ""


def test_subprocess_fails_closed_missing_allowlist() -> None:
    env = os.environ.copy()
    env["CODESPY_MCP_ORG_ID"] = str(uuid4())
    env["CODESPY_MCP_PRINCIPAL_ID"] = str(uuid4())
    env.pop("CODESPY_MCP_REPOSITORY_ALLOWLIST", None)
    proc = subprocess.run(  # noqa: S603 - trusted python binary and arguments
        [PYTHON_EXE, "-m", "app.mcp"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "CODESPY_MCP_REPOSITORY_ALLOWLIST environment variable is required" in proc.stderr
    assert proc.stdout == ""


def test_subprocess_stdio_handshake_and_tools_list() -> None:
    allowed_repo = uuid4()
    disallowed_repo = uuid4()
    org_id = uuid4()
    principal_id = uuid4()

    env = os.environ.copy()
    env["CODESPY_MCP_ORG_ID"] = str(org_id)
    env["CODESPY_MCP_PRINCIPAL_ID"] = str(principal_id)
    env["CODESPY_MCP_REPOSITORY_ALLOWLIST"] = str(allowed_repo)
    env["PYTHONPATH"] = BACKEND_DIR
    env.pop("CODESPY_DATABASE_URL", None)
    env.pop("CODESPY_SOURCE_ROOT", None)
    env.pop("CODESPY_MCP_SOURCE_IMAGE", None)
    env.pop("CODESPY_MCP_DOCKER_EXECUTABLE", None)
    env.pop("CODESPY_MCP_DOCKER_SOCKET", None)

    proc = subprocess.Popen(  # noqa: S603 - trusted python binary and arguments
        [PYTHON_EXE, "-m", "app.mcp"],
        cwd=BACKEND_DIR,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert proc.stdin is not None
    assert proc.stdout is not None

    try:
        # 1. Initialize
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        }
        proc.stdin.write(json.dumps(init_req) + "\n")
        proc.stdin.flush()

        init_line = proc.stdout.readline()
        assert init_line.strip()
        init_res = json.loads(init_line)
        assert init_res["id"] == 1
        assert init_res["result"]["serverInfo"]["name"] == "codespy-retrieval-mcp"

        # 2. Initialized notification
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        proc.stdin.write(json.dumps(notif) + "\n")
        proc.stdin.flush()

        # 3. List tools
        list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        proc.stdin.write(json.dumps(list_req) + "\n")
        proc.stdin.flush()

        list_line = proc.stdout.readline()
        assert list_line.strip()
        list_res = json.loads(list_line)
        assert list_res["id"] == 2
        tool_names = [t["name"] for t in list_res["result"]["tools"]]
        assert len(tool_names) == 13
        assert "get_index_status" in tool_names
        assert "get_source_view" in tool_names
        assert "search_exact" in tool_names
        assert "search_hybrid" in tool_names
        assert "assemble_context" in tool_names
        assert "query_memory" in tool_names
        assert "get_investigation_status" in tool_names
        assert "get_investigation_result" in tool_names
        assert "list_findings" in tool_names
        assert "get_evidence" in tool_names
        assert "graph_neighbors" in tool_names
        assert "trace_feature" in tool_names
        assert "list_claims" in tool_names

        # 4. Call tool with disallowed repository
        call_disallowed = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "get_index_status",
                "arguments": {
                    "repository_id": str(disallowed_repo),
                    "snapshot_id": str(uuid4()),
                    "job_id": str(uuid4()),
                },
            },
        }
        proc.stdin.write(json.dumps(call_disallowed) + "\n")
        proc.stdin.flush()

        call_line = proc.stdout.readline()
        assert call_line.strip()
        call_res = json.loads(call_line)
        assert call_res["id"] == 3
        assert call_res["result"]["isError"] is True
        assert call_res["result"]["structuredContent"]["code"] == "scope_denied"

    finally:
        # Close stdin to trigger EOF and clean shutdown
        proc.stdin.close()
        proc.wait(timeout=5)
        stderr_data = proc.stderr.read() if proc.stderr else ""
        assert proc.returncode == 0
        assert "CodeSPY MCP server shutdown completed." in stderr_data
