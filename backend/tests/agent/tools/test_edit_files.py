"""create_file, delete_path and move_path, and which tools each mode offers."""

import hashlib

from app.services.agent.tools import edit_tools, read_tools, tools_for_mode

from tests.agent.tools.support import AWS, FILES, call, make_context


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def test_create_file_writes_a_new_file_and_reports_its_hash():
    ctx, _ = await make_context()
    result = await call(ctx, "create_file", path="src/new.py", content="print('x')\n")
    assert result.status == "ok" and "create  src/new.py  +1 -0" in result.content
    assert (await ctx.sandbox.read_bytes(ctx.handle, "src/new.py")) == b"print('x')\n"
    assert result.data["files"][0]["after"] == sha(b"print('x')\n") and ctx.workspace.revision == 1
    assert "+print('x')" in result.content and "--- /dev/null" in result.content


async def test_create_file_in_a_new_directory_and_an_empty_file():
    ctx, _ = await make_context()
    assert (await call(ctx, "create_file", path="deep/er/dir/a.txt", content="a")).status == "ok"
    assert (await call(ctx, "create_file", path="empty.txt", content="")).status == "ok"
    assert await ctx.sandbox.read_bytes(ctx.handle, "empty.txt") == b""


async def test_create_file_over_an_existing_file_is_a_conflict_showing_it():
    ctx, _ = await make_context()
    result = await call(ctx, "create_file", path="src/util.py", content="Y = 2\n")
    assert result.status == "invalid" and result.error_code == "workspace_conflict"
    assert "You expected it to be absent" in result.content and "X = 1" in result.content
    assert await ctx.sandbox.read_bytes(ctx.handle, "src/util.py") == FILES["src/util.py"]


async def test_creating_a_file_that_already_has_exactly_that_content_is_a_no_op():
    ctx, _ = await make_context()
    first = await call(ctx, "create_file", path="a.txt", content="same")
    second = await call(ctx, "create_file", path="a.txt", content="same")
    assert (
        first.data["replayed"] is False
        and second.data["replayed"] is True
        and ctx.workspace.revision == 1
    )
    existing = await call(
        ctx, "create_file", path="src/util.py", content="X = 1\n"
    )  # identical to what is there
    assert existing.status == "ok" and existing.data["replayed"] is True


async def test_create_file_rejects_bad_paths_and_oversized_content():
    ctx, _ = await make_context()
    for path in ("../x", "/abs", ".git/hooks/pre-commit", "a//b"):
        assert (await call(ctx, "create_file", path=path, content="x")).status == "denied"
    assert (
        await call(ctx, "create_file", path="big.bin", content="x" * (1024 * 1024 + 1))
    ).error_code == "invalid_arguments"
    assert (await call(ctx, "create_file", path="src", content="x")).status in {
        "invalid",
        "denied",
    }  # a directory is not a file


async def test_delete_path_needs_the_hash_and_removes_the_file():
    ctx, _ = await make_context()
    stale = await call(ctx, "delete_path", path="src/util.py", expected_hash=sha(b"old"))
    assert stale.error_code == "workspace_conflict" and "X = 1" in stale.content
    result = await call(
        ctx, "delete_path", path="src/util.py", expected_hash=sha(FILES["src/util.py"])
    )
    assert result.status == "ok" and "delete  src/util.py  +0 -1" in result.content
    assert "src/util.py" not in await ctx.sandbox.file_hashes(ctx.handle)
    assert (
        "No change"
        in (
            await call(
                ctx, "delete_path", path="src/util.py", expected_hash=sha(FILES["src/util.py"])
            )
        ).content
    )


async def test_delete_path_refuses_missing_files_credentials_and_traversal():
    ctx, _ = await make_context()
    assert (
        await call(ctx, "delete_path", path="nope.py", expected_hash=sha(b"x"))
    ).error_code == "file_not_found"
    assert (
        await call(ctx, "delete_path", path=".env", expected_hash=sha(FILES[".env"]))
    ).error_code == "sensitive_path"
    assert (await call(ctx, "delete_path", path="../x", expected_hash=sha(b"x"))).status == "denied"
    assert (await ctx.sandbox.read_bytes(ctx.handle, ".env")) == FILES[".env"]


async def test_move_path_renames_and_reports_both_names():
    ctx, _ = await make_context()
    result = await call(
        ctx,
        "move_path",
        path="src/util.py",
        to="lib/helpers.py",
        expected_hash=sha(FILES["src/util.py"]),
    )
    assert (
        result.status == "ok"
        and "rename  lib/helpers.py  +0 -0  (from src/util.py)" in result.content
    )
    hashes = await ctx.sandbox.file_hashes(ctx.handle)
    assert "src/util.py" not in hashes and hashes["lib/helpers.py"] == sha(FILES["src/util.py"])
    again = await call(
        ctx,
        "move_path",
        path="src/util.py",
        to="lib/helpers.py",
        expected_hash=sha(FILES["src/util.py"]),
    )
    assert again.status == "ok" and again.data["replayed"] is True


async def test_move_path_refuses_an_existing_destination_a_stale_hash_and_credentials():
    ctx, _ = await make_context()
    taken = await call(
        ctx,
        "move_path",
        path="src/util.py",
        to="src/app.py",
        expected_hash=sha(FILES["src/util.py"]),
    )
    assert taken.error_code == "destination_exists"
    stale = await call(ctx, "move_path", path="src/util.py", to="x.py", expected_hash=sha(b"old"))
    assert stale.error_code == "workspace_conflict"
    assert (
        await call(ctx, "move_path", path=".env", to="env.txt", expected_hash=sha(FILES[".env"]))
    ).error_code == "sensitive_path"
    assert (
        await call(
            ctx,
            "move_path",
            path="src/util.py",
            to=".env.local",
            expected_hash=sha(FILES["src/util.py"]),
        )
    ).error_code == "sensitive_path"
    assert (
        await call(
            ctx,
            "move_path",
            path="src/util.py",
            to="../out",
            expected_hash=sha(FILES["src/util.py"]),
        )
    ).status == "denied"


async def test_each_edit_is_one_revision_and_the_session_status_follows():
    ctx, _ = await make_context()
    await call(ctx, "create_file", path="a.txt", content="a")
    await call(ctx, "move_path", path="a.txt", to="b.txt", expected_hash=sha(b"a"))
    await call(ctx, "delete_path", path="b.txt", expected_hash=sha(b"a"))
    assert ctx.workspace.revision == 3
    assert (await call(ctx, "git_status")).content == "no changes"


def test_plan_mode_offers_only_read_tools_and_act_mode_adds_the_edit_tools():
    plan = {s.name for s in tools_for_mode("plan").specs()}
    act = {s.name for s in tools_for_mode("act").specs()}
    assert plan == {s.name for s in read_tools().specs()} and not plan & {
        s.name for s in edit_tools().specs()
    }
    assert act == plan | {"apply_patch", "create_file", "delete_path", "move_path"}
    assert {s.tool_class for s in edit_tools().specs()} == {"edit"}


async def test_a_plan_mode_registry_refuses_an_edit_tool_it_was_not_given():
    ctx, _ = await make_context()
    result = await tools_for_mode("plan").execute(
        ctx, "create_file", '{"path": "a.txt", "content": "x"}'
    )
    assert result.error_code == "unknown_tool" and "a.txt" not in await ctx.sandbox.file_hashes(
        ctx.handle
    )


async def test_edit_results_never_contain_secrets_from_created_content():
    ctx, _ = await make_context()
    result = await call(ctx, "create_file", path="cfg.py", content=f'KEY = "{AWS}"\n')
    assert result.status == "ok" and AWS not in result.model_dump_json() and result.redactions >= 1
