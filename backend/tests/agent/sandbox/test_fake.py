"""Behaviour specific to the fake: scripted commands, their effects, and call recording."""

from uuid import uuid4

from app.adapters.sandbox.fake import FakeSandbox, MappingSnapshots, ScriptedExec
from app.contracts.workspace import ExecRequestV1, SandboxSpecV1, SnapshotRefV1
from app.ports.sandbox import normalize_workspace_path, tree_digest

SNAP = uuid4()


async def started(files=None, **kw):
    sandbox = FakeSandbox(MappingSnapshots({SNAP: files or {"a.txt": b"a\n"}}), **kw)
    org = uuid4()
    handle = await sandbox.acquire(
        SandboxSpecV1(org_id=org, session_id=uuid4(), image="test:latest")
    )
    await sandbox.materialize(
        handle, SnapshotRefV1(org_id=org, repository_id=uuid4(), snapshot_id=SNAP)
    )
    return sandbox, handle


async def collect(sandbox, handle, *argv, **kw):
    return [c async for c in sandbox.exec(handle, ExecRequestV1(argv=argv, **kw))]


async def test_an_unscripted_command_is_not_found_and_calls_are_recorded():
    sandbox, handle = await started()
    chunks = await collect(sandbox, handle, "make", "test")
    assert chunks[-1].exit_code == 127 and "command not found" in chunks[0].text
    assert [c.argv for c in sandbox.exec_calls] == [("make", "test")]


async def test_the_most_specific_latest_script_wins_by_prefix():
    sandbox, handle = await started()
    sandbox.script(("pytest",), ScriptedExec(stdout="all\n"))
    sandbox.script(("pytest", "-k", "one"), ScriptedExec(stdout="one\n", exit_code=1))
    assert (await collect(sandbox, handle, "pytest", "-k", "one"))[-1].exit_code == 1
    assert (await collect(sandbox, handle, "pytest"))[0].text == "all\n"


async def test_a_script_can_write_files_like_a_build_does():
    sandbox, handle = await started()
    before = await sandbox.status(handle)
    sandbox.script(("build",), ScriptedExec(writes={"dist/out.js": b"1"}))
    await collect(sandbox, handle, "build")
    after = await sandbox.status(handle)
    assert after.revision == before.revision + 1 and after.file_count == before.file_count + 1
    assert (await sandbox.read_file(handle, "dist/out.js")).content == "1"


async def test_a_timed_out_command_does_not_apply_its_writes():
    sandbox, handle = await started()
    sandbox.script(("slow",), ScriptedExec(delay_s=3, writes={"late.txt": b"x"}))
    chunks = await collect(sandbox, handle, "slow", timeout_s=1)
    assert chunks[-1].timed_out and (await sandbox.status(handle)).file_count == 1


async def test_a_sandbox_without_suspend_keeps_running():
    sandbox, handle = await started(supports_suspend=False)
    assert not sandbox.capabilities.supports_suspend
    await sandbox.suspend(handle)
    assert (await sandbox.status(handle)).file_count == 1


def test_path_normalisation_is_canonical():
    assert normalize_workspace_path("./a/b.py") == "a/b.py"
    assert normalize_workspace_path(".", allow_root=True) == "."
    assert normalize_workspace_path("src/é.py") == "src/é.py"


def test_tree_digest_depends_on_content_and_paths_but_not_order():
    a = {"x": "1" * 64, "y": "2" * 64}
    assert tree_digest(a) == tree_digest({"y": "2" * 64, "x": "1" * 64})
    assert tree_digest(a) != tree_digest({"x": "1" * 64, "z": "2" * 64})
    assert tree_digest(a) != tree_digest({"x": "1" * 64, "y": "3" * 64})
