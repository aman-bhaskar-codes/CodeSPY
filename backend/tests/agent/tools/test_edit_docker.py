# ruff: noqa: E501
"""The edit tools against real containers: edits are visible to commands, and commands' changes conflict with stale edits."""

import hashlib
import json
from uuid import uuid4

import pytest
from app.contracts.workspace import ExecRequestV1, SandboxSpecV1, SnapshotRefV1
from app.services.agent.tools import tools_for_mode
from app.services.agent.tools.base import ToolContext
from app.services.workspace.local import LocalWorkspace

from tests.agent.sandbox.docker_support import IMAGE, Harness, requires_docker

pytestmark = [pytest.mark.sandbox, requires_docker]

SNAP = uuid4()
FILES = {
    "app.py": b"print('one')\n",
    "run.sh": b"#!/bin/sh\necho hello\n",
    "win.txt": b"a\r\nb\r\n",
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
async def ctx():
    harness = Harness({SNAP: FILES})
    org = uuid4()
    handle = await harness.sandbox.acquire(
        SandboxSpecV1(org_id=org, session_id=uuid4(), image=IMAGE)
    )
    await harness.sandbox.materialize(
        handle, SnapshotRefV1(org_id=org, repository_id=uuid4(), snapshot_id=SNAP)
    )
    yield ToolContext(
        sandbox=harness.sandbox, handle=handle, workspace=LocalWorkspace(harness.sandbox, handle)
    )
    await harness.close()


async def run(ctx, *argv):
    chunks = [c async for c in ctx.sandbox.exec(ctx.handle, ExecRequestV1(argv=argv, timeout_s=30))]
    return "".join(c.text for c in chunks if c.kind == "stdout"), chunks[-1].exit_code


async def call(ctx, name, **args):
    return await tools_for_mode("act").execute(ctx, name, json.dumps(args))


async def test_edit_then_run(ctx):
    result = await call(
        ctx,
        "apply_patch",
        edits=[
            {
                "path": "app.py",
                "expected_hash": sha(FILES["app.py"]),
                "search": "one",
                "replace": "two",
            }
        ],
    )
    assert result.status == "ok"
    assert await run(ctx, "python3", "app.py") == ("two\n", 0)
    created = await call(ctx, "create_file", path="pkg/mod.py", content="VALUE = 41 + 1\n")
    assert created.status == "ok"
    assert await run(ctx, "python3", "-c", "import pkg.mod as m; print(m.VALUE)") == ("42\n", 0)


async def test_a_patch_keeps_the_executable_bit_and_windows_line_endings(ctx):
    await call(
        ctx,
        "apply_patch",
        edits=[
            {
                "path": "run.sh",
                "expected_hash": sha(FILES["run.sh"]),
                "search": "hello",
                "replace": "patched",
            }
        ],
    )
    await run(ctx, "chmod", "+x", "run.sh")
    again = await call(
        ctx,
        "apply_patch",
        edits=[
            {
                "path": "run.sh",
                "expected_hash": sha(b"#!/bin/sh\necho patched\n"),
                "search": "patched",
                "replace": "twice",
            }
        ],
    )
    assert again.status == "ok" and await run(ctx, "./run.sh") == (
        "twice\n",
        0,
    )  # still executable after an edit
    await call(
        ctx,
        "apply_patch",
        edits=[
            {
                "path": "win.txt",
                "expected_hash": sha(FILES["win.txt"]),
                "search": "a\nb",
                "replace": "a\nB",
            }
        ],
    )
    assert (await ctx.sandbox.read_bytes(ctx.handle, "win.txt")) == b"a\r\nB\r\n"


async def test_a_command_that_changed_the_file_makes_the_agents_edit_a_conflict(ctx):
    await run(ctx, "sh", "-c", "echo 'print(\"changed by a command\")' > app.py")
    result = await call(
        ctx,
        "apply_patch",
        edits=[
            {
                "path": "app.py",
                "expected_hash": sha(FILES["app.py"]),
                "search": "one",
                "replace": "two",
            }
        ],
    )
    assert result.error_code == "workspace_conflict" and "changed by a command" in result.content
    fresh = result.data["conflicts"][0]["actual_hash"]
    retry = await call(
        ctx,
        "apply_patch",
        edits=[
            {
                "path": "app.py",
                "expected_hash": fresh,
                "search": "changed by a command",
                "replace": "edited after",
            }
        ],
    )
    assert retry.status == "ok" and await run(ctx, "python3", "app.py") == ("edited after\n", 0)


async def test_replaying_an_applied_patch_does_nothing_on_a_real_workspace(ctx):
    patch = {
        "edits": [
            {
                "path": "app.py",
                "expected_hash": sha(FILES["app.py"]),
                "search": "one",
                "replace": "two",
            }
        ]
    }
    first = await call(ctx, "apply_patch", **patch)
    before = await ctx.sandbox.file_hashes(ctx.handle)
    second = await call(ctx, "apply_patch", **patch)
    assert first.data["replayed"] is False and second.data["replayed"] is True
    assert await ctx.sandbox.file_hashes(ctx.handle) == before and ctx.workspace.revision == 1


async def test_a_multi_file_patch_is_atomic_on_a_real_workspace(ctx):
    before = await ctx.sandbox.file_hashes(ctx.handle)
    bad = await call(
        ctx,
        "apply_patch",
        edits=[
            {
                "path": "app.py",
                "expected_hash": sha(FILES["app.py"]),
                "search": "one",
                "replace": "two",
            },
            {
                "path": "run.sh",
                "expected_hash": sha(FILES["run.sh"]),
                "search": "NOT THERE",
                "replace": "x",
            },
        ],
    )
    assert bad.status == "invalid" and await ctx.sandbox.file_hashes(ctx.handle) == before


async def test_move_and_delete_on_a_real_workspace(ctx):
    moved = await call(
        ctx, "move_path", path="app.py", to="src/app.py", expected_hash=sha(FILES["app.py"])
    )
    assert moved.status == "ok" and await run(ctx, "python3", "src/app.py") == ("one\n", 0)
    gone = await call(ctx, "delete_path", path="src/app.py", expected_hash=sha(FILES["app.py"]))
    assert gone.status == "ok" and (await run(ctx, "ls", "src"))[0] == ""
