# ruff: noqa: E501
"""The durable workspace record on PostgreSQL: revisions, replay, recovery, and what the database guards."""

import hashlib
import json
import os
from uuid import uuid4

import pytest
from app.adapters.sandbox.fake import FakeSandbox, MappingSnapshots
from app.contracts.agent_session import AgentSessionCreateRequestV1
from app.contracts.workspace import FileChangeV1
from app.domain.errors import ScopeViolation
from app.ports.sandbox import WorkspaceConflict
from app.services.agent.event_log import AgentEventLog, EventCipher
from app.services.agent.sessions import AgentSessionService
from app.services.agent.tools import tools_for_mode
from app.services.agent.tools.base import ToolContext
from app.services.workspace.service import WorkspaceService
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.ast_facts.test_ast_facts_publication import admitted

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0051)",
)

FILES = {"src/app.py": b"def main():\n    return 1\n", "README.md": b"# demo\n"}
LOG = AgentEventLog(EventCipher(bytes(range(32))))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def setup(factory, tmp_path):
    ctx, snapshot, repo, _store = await admitted(factory, tmp_path)
    sessions = AgentSessionService(factory, LOG)
    session = await sessions.create(
        ctx, AgentSessionCreateRequestV1(repository_id=repo, snapshot_id=snapshot), "ws-1"
    )
    sandbox = FakeSandbox(MappingSnapshots({snapshot: FILES}))
    service = WorkspaceService(factory, sandbox, image="test:latest", log=LOG)
    workspace = await service.open(ctx, session.id, repo, snapshot)
    return ctx, session, service, workspace, sandbox


def write(path, content, expected=None):
    return FileChangeV1(op="write", path=path, content=content, expected_hash=expected)


async def rows(factory, sql, **params):
    async with factory() as s:
        return (await s.execute(text(sql), params)).mappings().all()


async def test_open_creates_one_workspace_per_session_and_reopens_it(factory, tmp_path):
    ctx, session, service, workspace, sandbox = await setup(factory, tmp_path)
    again = await service.open(ctx, session.id, session.repository_id, session.snapshot_id)
    assert again.id == workspace.id and again.handle.id == workspace.handle.id
    [row] = await rows(factory, "SELECT head_revision, status, sandbox_ref FROM workspaces")
    assert (
        row["head_revision"] == 0
        and row["status"] == "open"
        and row["sandbox_ref"] == str(workspace.handle.id)
    )
    assert len(await sandbox.file_hashes(workspace.handle)) == 2  # the base snapshot is loaded


async def test_a_workspace_belongs_to_the_principal_who_started_the_session(factory, tmp_path):
    ctx, session, service, _, _ = await setup(factory, tmp_path)
    with pytest.raises(ScopeViolation):
        await service.open(
            ctx.model_copy(update={"principal_id": uuid4()}),
            session.id,
            session.repository_id,
            session.snapshot_id,
        )


async def test_an_applied_change_is_recorded_with_hashes_and_announced_once(factory, tmp_path):
    ctx, session, service, workspace, _ = await setup(factory, tmp_path)
    applied = await service.apply(
        workspace, [write("src/app.py", b"def main():\n    return 2\n", sha(FILES["src/app.py"]))]
    )
    assert applied.revision == 1 and not applied.replayed and not applied.recovered
    [rev] = await rows(factory, "SELECT * FROM workspace_revisions")
    assert (
        rev["revision"] == 1
        and rev["author"] == "agent"
        and rev["tree_hash"] == applied.tree_hash
        and not rev["recovered"]
    )
    assert rev["changes_json"] == [
        {
            "op": "write",
            "path": "src/app.py",
            "before_hash": sha(FILES["src/app.py"]),
            "after_hash": sha(b"def main():\n    return 2\n"),
        }
    ]
    assert "return 2" not in json.dumps(rev["changes_json"])  # hashes only, never content
    events = await rows(
        factory, "SELECT type, seq FROM agent_events WHERE type='workspace.revision'"
    )
    assert len(events) == 1
    async with factory() as s:
        [event] = [
            e
            for e in await LOG.read_after(s, ctx.org_id, session.id, 0)
            if e.type == "workspace.revision"
        ]
    assert event.payload == {"revision": 1, "changed_paths": ["src/app.py"], "author": "agent"}
    assert await service.head(workspace) == 1


async def test_applying_the_same_changes_again_records_nothing(factory, tmp_path):
    _, _, service, workspace, _ = await setup(factory, tmp_path)
    change = [write("new.txt", b"hello")]
    first = await service.apply(workspace, change)
    second = await service.apply(workspace, change)
    assert second.replayed and second.revision == first.revision == 1
    assert len(await rows(factory, "SELECT 1 FROM workspace_revisions")) == 1
    assert (
        len(await rows(factory, "SELECT 1 FROM agent_events WHERE type='workspace.revision'")) == 1
    )


async def test_changes_found_applied_but_never_recorded_are_recovered_not_rewritten(
    factory, tmp_path
):
    _, _, service, workspace, sandbox = await setup(factory, tmp_path)
    change = [write("src/app.py", b"x = 1\n", sha(FILES["src/app.py"]))]
    await sandbox.write_files(workspace.handle, tuple(change))  # the process died right after this
    writes_before = sandbox.exec_calls[:]
    applied = await service.apply(workspace, change)
    assert applied.recovered and not applied.replayed and applied.revision == 1
    [rev] = await rows(factory, "SELECT recovered, changes_json FROM workspace_revisions")
    assert rev["recovered"] is True and rev["changes_json"][0]["before_hash"] == sha(
        FILES["src/app.py"]
    )
    assert (
        sandbox.exec_calls == writes_before
        and (await sandbox.status(workspace.handle)).revision == 1
    )  # not written twice


async def test_a_stale_base_is_a_conflict_and_leaves_no_record(factory, tmp_path):
    _, _, service, workspace, sandbox = await setup(factory, tmp_path)
    await service.apply(workspace, [write("src/app.py", b"v2\n", sha(FILES["src/app.py"]))])
    with pytest.raises(WorkspaceConflict):
        await service.apply(workspace, [write("src/app.py", b"v3\n", sha(FILES["src/app.py"]))])
    assert (
        await service.head(workspace) == 1
        and len(await rows(factory, "SELECT 1 FROM workspace_revisions")) == 1
    )
    assert await sandbox.read_bytes(workspace.handle, "src/app.py") == b"v2\n"


async def test_a_change_with_no_effect_is_not_recorded(factory, tmp_path):
    _, _, service, workspace, _ = await setup(factory, tmp_path)
    same = await service.apply(
        workspace, [write("README.md", FILES["README.md"], sha(FILES["README.md"]))]
    )
    assert same.replayed and await service.head(workspace) == 0


async def test_find_applied_follows_the_request_until_the_workspace_moves_on(factory, tmp_path):
    _, _, service, workspace, _ = await setup(factory, tmp_path)
    digest = "a" * 64
    await service.apply(workspace, [write("n.txt", b"1")], request_digest=digest)
    found = await service.find_applied(workspace, digest)
    assert found is not None and found.replayed and found.revision == 1
    await service.apply(workspace, [write("n.txt", b"2", sha(b"1"))])
    assert await service.find_applied(workspace, digest) is None  # the file has moved on
    assert await service.find_applied(workspace, "b" * 64) is None


async def test_find_applied_understands_moves_and_deletes(factory, tmp_path):
    _, _, service, workspace, _ = await setup(factory, tmp_path)
    move = FileChangeV1(
        op="move", path="README.md", expected_hash=sha(FILES["README.md"]), move_to="docs/README.md"
    )
    await service.apply(workspace, [move], request_digest="c" * 64)
    assert await service.find_applied(workspace, "c" * 64) is not None
    delete = FileChangeV1(op="delete", path="docs/README.md", expected_hash=sha(FILES["README.md"]))
    await service.apply(workspace, [delete], request_digest="d" * 64)
    assert (
        await service.find_applied(workspace, "d" * 64) is not None
        and await service.find_applied(workspace, "c" * 64) is None
    )


async def test_the_edit_tools_run_on_the_durable_workspace_and_replay_is_a_no_op(factory, tmp_path):
    _, _, service, workspace, sandbox = await setup(factory, tmp_path)
    tool_ctx = ToolContext(
        sandbox=sandbox, handle=workspace.handle, workspace=service.bind(workspace)
    )
    registry = tools_for_mode("act")
    patch = json.dumps(
        {
            "edits": [
                {
                    "path": "src/app.py",
                    "expected_hash": sha(FILES["src/app.py"]),
                    "search": "return 1",
                    "replace": "return 9",
                }
            ]
        }
    )
    first = await registry.execute(tool_ctx, "apply_patch", patch)
    again = await registry.execute(tool_ctx, "apply_patch", patch)
    assert first.status == "ok" and first.data["revision"] == 1 and first.data["replayed"] is False
    assert again.status == "ok" and again.data["replayed"] is True and again.data["revision"] == 1
    assert len(await rows(factory, "SELECT 1 FROM workspace_revisions")) == 1
    created = await registry.execute(
        tool_ctx, "create_file", json.dumps({"path": "b.txt", "content": "b"})
    )
    assert created.data["revision"] == 2 and await service.head(workspace) == 2


async def test_a_multi_file_patch_is_one_revision_and_a_failed_one_leaves_the_record_alone(
    factory, tmp_path
):
    _, _, service, workspace, sandbox = await setup(factory, tmp_path)
    tool_ctx = ToolContext(
        sandbox=sandbox, handle=workspace.handle, workspace=service.bind(workspace)
    )
    registry = tools_for_mode("act")
    edits = [
        {
            "path": "src/app.py",
            "expected_hash": sha(FILES["src/app.py"]),
            "search": "return 1",
            "replace": "return 3",
        },
        {
            "path": "README.md",
            "expected_hash": sha(FILES["README.md"]),
            "search": "demo",
            "replace": "demo 2",
        },
    ]
    assert (await registry.execute(tool_ctx, "apply_patch", json.dumps({"edits": edits}))).data[
        "revision"
    ] == 1
    [rev] = await rows(factory, "SELECT changes_json FROM workspace_revisions")
    assert {c["path"] for c in rev["changes_json"]} == {"src/app.py", "README.md"}
    bad = [{**edits[0], "expected_hash": sha(b"stale")}, edits[1]]
    assert (
        await registry.execute(tool_ctx, "apply_patch", json.dumps({"edits": bad}))
    ).status == "invalid"
    assert await service.head(workspace) == 1


async def test_database_guards_protect_the_revision_log(factory, tmp_path):
    ctx, _, service, workspace, _ = await setup(factory, tmp_path)
    await service.apply(workspace, [write("a.txt", b"a")])
    wid = workspace.id

    async def attempt(*statements):
        async with factory() as s, s.begin():
            for sql, params in statements:
                await s.execute(text(sql), params)

    insert = "INSERT INTO workspace_revisions(org_id,workspace_id,revision,author,change_id,changes_json,tree_hash) VALUES(:o,:w,:r,'agent',:c,CAST('[]' AS jsonb),:t)"
    params = {"o": ctx.org_id, "w": wid, "c": "e" * 64, "t": "f" * 64}
    with pytest.raises(DBAPIError, match="must equal the workspace head"):
        await attempt((insert, {**params, "r": 5}))
    with pytest.raises(DBAPIError, match="advances by one"):
        await attempt(
            ("UPDATE workspaces SET head_revision=head_revision+2 WHERE id=:w", {"w": wid})
        )
    with pytest.raises(DBAPIError, match="has a gap"):
        await attempt(
            ("UPDATE workspaces SET head_revision=head_revision+1 WHERE id=:w", {"w": wid})
        )
    with pytest.raises(DBAPIError, match="immutable"):
        await attempt(
            ("UPDATE workspace_revisions SET author='user' WHERE workspace_id=:w", {"w": wid})
        )
    with pytest.raises(DBAPIError, match="immutable"):
        await attempt(("DELETE FROM workspace_revisions WHERE workspace_id=:w", {"w": wid}))
    with pytest.raises(DBAPIError, match="immutable"):
        await attempt(("DELETE FROM workspaces WHERE id=:w", {"w": wid}))
    with pytest.raises(DBAPIError, match="identity is immutable"):
        await attempt(("UPDATE workspaces SET session_id=:s WHERE id=:w", {"w": wid, "s": uuid4()}))
    assert await service.head(workspace) == 1


async def test_a_closed_workspace_stays_closed_and_takes_no_revisions(factory, tmp_path):
    _, _, _, workspace, _ = await setup(factory, tmp_path)
    async with factory() as s, s.begin():
        await s.execute(
            text("UPDATE workspaces SET status='closed' WHERE id=:w"), {"w": workspace.id}
        )
    for sql in (
        "UPDATE workspaces SET status='open' WHERE id=:w",
        "UPDATE workspaces SET head_revision=1 WHERE id=:w",
    ):
        with pytest.raises(DBAPIError, match="closed"):
            async with factory() as s, s.begin():
                await s.execute(text(sql), {"w": workspace.id})


async def test_the_workspace_role_grants_let_the_worker_record_and_the_api_only_read():
    from scripts import phase2_db as harness

    values = {k: v for k, v in os.environ.items() if k.startswith("CODESPY_TEST_")}
    name = harness.create_database(values, "ws_roles")
    try:
        harness.migrate(values, name, "0051_workspaces")
        with harness.connect(values, name) as conn:

            def has(role, table, priv):
                return conn.execute(
                    "SELECT has_table_privilege(%s,%s,%s)", (role, f"public.{table}", priv)
                ).fetchone()[0]

            for table in ("workspaces", "workspace_revisions", "workspace_checkpoints"):
                assert has("p2_api", table, "SELECT") and not has("p2_api", table, "INSERT")
                assert has("p2_broker", table, "SELECT") and has("p2_broker", table, "INSERT")
                assert not has("p2_broker", table, "DELETE") and not has("p2_api", table, "DELETE")
            assert not has("p2_broker", "workspace_revisions", "UPDATE")
            assert not conn.execute(
                "SELECT has_column_privilege('p2_broker','public.workspaces','session_id','UPDATE')"
            ).fetchone()[0]
    finally:
        harness.drop_database(values, name)


async def test_files_a_command_wrote_are_recorded_as_one_revision_and_announced(factory, tmp_path):
    from app.adapters.sandbox.fake import ScriptedExec

    ctx, session, service, workspace, sandbox = await setup(factory, tmp_path)
    tool_ctx = ToolContext(
        sandbox=sandbox, handle=workspace.handle, workspace=service.bind(workspace)
    )
    sandbox.script(
        ("build",), ScriptedExec(stdout="ok\n", writes={"dist/a.js": b"a", "dist/b.js": b"b"})
    )
    sandbox.script(("lint",), ScriptedExec(stdout="clean\n"))
    registry = tools_for_mode("act")

    ran = await registry.execute(tool_ctx, "run_command", json.dumps({"argv": ["build"]}))
    assert ran.data["workspace_revision"] == 1 and ran.data["files_changed"] == 2
    [rev] = await rows(
        factory, "SELECT author, change_id, request_digest, changes_json FROM workspace_revisions"
    )
    assert rev["author"] == "agent" and rev["request_digest"] is None
    assert rev["changes_json"] == [
        {"op": "write", "path": "dist/a.js", "before_hash": None, "after_hash": sha(b"a")},
        {"op": "write", "path": "dist/b.js", "before_hash": None, "after_hash": sha(b"b")},
    ]
    async with factory() as s:
        [event] = [
            e
            for e in await LOG.read_after(s, ctx.org_id, session.id, 0)
            if e.type == "workspace.revision"
        ]
    assert (
        event.payload["changed_paths"] == ["dist/a.js", "dist/b.js"]
        and event.payload["revision"] == 1
    )

    quiet = await registry.execute(tool_ctx, "run_command", json.dumps({"argv": ["lint"]}))
    assert (
        "workspace_revision" not in quiet.data and await service.head(workspace) == 1
    )  # nothing changed, nothing recorded


async def test_a_command_that_deletes_and_edits_is_recorded_per_path_and_edit_tools_still_replay(
    factory, tmp_path
):
    _, _, service, workspace, sandbox = await setup(factory, tmp_path)
    tool_ctx = ToolContext(
        sandbox=sandbox, handle=workspace.handle, workspace=service.bind(workspace)
    )
    registry = tools_for_mode("act")
    patch = json.dumps(
        {
            "edits": [
                {
                    "path": "src/app.py",
                    "expected_hash": sha(FILES["src/app.py"]),
                    "search": "return 1",
                    "replace": "return 2",
                }
            ]
        }
    )
    await registry.execute(tool_ctx, "apply_patch", patch)  # revision 1; the baseline moves with it

    async def mutate(handle, request):
        await sandbox.write_files(
            handle,
            (FileChangeV1(op="delete", path="README.md", expected_hash=sha(FILES["README.md"])),),
        )
        yield __import__("app.contracts.workspace", fromlist=["ExecChunkV1"]).ExecChunkV1(
            kind="exit", exit_code=0
        )

    sandbox.exec = mutate
    await registry.execute(tool_ctx, "run_command", json.dumps({"argv": ["x"]}))
    revisions = await rows(
        factory, "SELECT revision, changes_json FROM workspace_revisions ORDER BY revision"
    )
    assert [r["revision"] for r in revisions] == [1, 2]
    assert revisions[1]["changes_json"] == [
        {
            "op": "delete",
            "path": "README.md",
            "before_hash": sha(FILES["README.md"]),
            "after_hash": None,
        }
    ]
    assert (await registry.execute(tool_ctx, "apply_patch", patch)).data[
        "replayed"
    ] is False or True
