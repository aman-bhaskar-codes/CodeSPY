# ruff: noqa: E501
"""apply_patch: exact SEARCH/REPLACE edits, all or nothing, conflicts that show the current text, replay as a no-op."""

import hashlib

from tests.agent.tools.support import AWS, FILES, call, edit, make_context


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


APP = sha(FILES["src/app.py"])
UTIL = sha(FILES["src/util.py"])


def e(path, expected, search, replace, **kw):
    return {"path": path, "expected_hash": expected, "search": search, "replace": replace, **kw}


async def text(ctx, path):
    return (await ctx.sandbox.read_bytes(ctx.handle, path)).decode()


async def test_a_single_edit_is_applied_and_reported_with_its_diff():
    ctx, _ = await make_context()
    result = await call(
        ctx, "apply_patch", edits=[e("src/app.py", APP, "return handle()", "return handle() + 1")]
    )
    assert result.status == "ok" and "Applied (workspace revision 1)" in result.content
    assert (
        "modify  src/app.py  +1 -1" in result.content
        and "-    return handle()\n+    return handle() + 1" in result.content
    )
    assert await text(ctx, "src/app.py") == FILES["src/app.py"].decode().replace(
        "return handle()", "return handle() + 1"
    )
    assert result.data["files"][0]["before"] == APP and result.data["files"][0]["after"] == sha(
        await ctx.sandbox.read_bytes(ctx.handle, "src/app.py")
    )


async def test_several_edits_to_one_file_apply_in_order_against_the_running_text():
    ctx, _ = await make_context()
    result = await call(
        ctx,
        "apply_patch",
        edits=[
            e("src/app.py", APP, "def main():", "def main(flag):"),
            e(
                "src/app.py",
                APP,
                "def main(flag):\n    return handle()",
                "def main(flag):\n    return handle(flag)",
            ),
            e("src/app.py", APP, "return 1", "return 2"),
        ],
    )
    assert result.status == "ok"
    assert (
        await text(ctx, "src/app.py")
        == "def main(flag):\n    return handle(flag)\n\n\ndef handle():\n    return 2\n"
    )


async def test_a_multi_file_patch_is_atomic_when_every_edit_is_valid():
    ctx, _ = await make_context()
    result = await call(
        ctx,
        "apply_patch",
        edits=[
            e("src/app.py", APP, "return 1", "return 7"),
            e("src/util.py", UTIL, "X = 1", "X = 70"),
        ],
    )
    assert (
        result.status == "ok" and result.data["revision"] == 1
    )  # one revision for the whole patch
    assert "X = 70" in await text(ctx, "src/util.py") and "return 7" in await text(
        ctx, "src/app.py"
    )


async def test_a_multi_file_patch_changes_nothing_when_any_edit_fails():
    ctx, _ = await make_context()
    before = await ctx.sandbox.file_hashes(ctx.handle)
    bad = await call(
        ctx,
        "apply_patch",
        edits=[
            e("src/app.py", APP, "return 1", "return 7"),
            e("src/util.py", UTIL, "NOT PRESENT", "x"),
        ],
    )
    assert (
        bad.status == "invalid"
        and bad.error_code == "patch_failed"
        and "no file changed" in bad.content
    )
    assert "src/util.py edit 1: the search text was not found" in bad.content
    assert (
        await ctx.sandbox.file_hashes(ctx.handle) == before
    )  # the valid edit to app.py was not applied either
    assert ctx.workspace.revision == 0


async def test_a_search_that_is_not_found_names_the_closest_line():
    ctx, _ = await make_context()
    result = await call(
        ctx, "apply_patch", edits=[e("src/app.py", APP, "def mian():", "def main():")]
    )
    assert result.status == "invalid" and "closest existing line is 'def main():'" in result.content


async def test_an_ambiguous_search_lists_where_it_matches_and_occurrence_resolves_it():
    files = {"a.py": b"x = 1\ny = 1\nz = 1\n"}
    ctx, _ = await make_context(files=files)
    digest = sha(files["a.py"])
    vague = await call(ctx, "apply_patch", edits=[e("a.py", digest, "= 1", "= 2")])
    assert vague.status == "invalid" and "appears 3 times (lines 1, 2, 3)" in vague.content
    second = await call(ctx, "apply_patch", edits=[e("a.py", digest, "= 1", "= 2", occurrence=2)])
    assert second.status == "ok" and await text(ctx, "a.py") == "x = 1\ny = 2\nz = 1\n"
    out = await call(
        ctx,
        "apply_patch",
        edits=[e("a.py", sha(b"x = 1\ny = 2\nz = 1\n"), "= 1", "= 3", occurrence=5)],
    )
    assert "occurrence 5 requested but the text appears 2" in out.content


async def test_matching_is_exact_including_whitespace():
    ctx, _ = await make_context()
    assert (
        await call(ctx, "apply_patch", edits=[e("src/app.py", APP, "return  1", "return 2")])
    ).status == "invalid"
    assert (
        await call(ctx, "apply_patch", edits=[e("src/app.py", APP, "RETURN 1", "return 2")])
    ).status == "invalid"


async def test_crlf_files_are_edited_with_their_line_endings_preserved():
    files = {"w.txt": b"one\r\ntwo\r\nthree\r\n"}
    ctx, _ = await make_context(files=files)
    result = await call(
        ctx, "apply_patch", edits=[e("w.txt", sha(files["w.txt"]), "one\ntwo", "one\nTWO\nextra")]
    )
    assert result.status == "ok" and await text(ctx, "w.txt") == "one\r\nTWO\r\nextra\r\nthree\r\n"


async def test_an_edit_that_changes_nothing_is_refused():
    ctx, _ = await make_context()
    result = await call(ctx, "apply_patch", edits=[e("src/app.py", APP, "return 1", "return 1")])
    assert result.status == "invalid" and "the edits change nothing" in result.content


async def test_a_stale_hash_is_a_structured_conflict_with_the_current_text_around_the_edit():
    ctx, _ = await make_context()
    changed = FILES["big.txt"].replace(b"line 1000\n", b"line 1000 CHANGED BY SOMEONE ELSE\n")
    await edit(ctx, "big.txt", changed, sha(FILES["big.txt"]))
    result = await call(
        ctx,
        "apply_patch",
        edits=[e("big.txt", sha(FILES["big.txt"]), "line 1000", "line 1000 mine")],
    )
    assert result.status == "invalid" and result.error_code == "workspace_conflict"
    assert result.data["conflicts"] == [
        {"path": "big.txt", "expected_hash": sha(FILES["big.txt"]), "actual_hash": sha(changed)}
    ]
    assert (
        "You expected sha256" in result.content
        and f"it is now sha256 {sha(changed)}" in result.content
    )
    assert (
        "line 1000 CHANGED BY SOMEONE ELSE" in result.content and "line 999" in result.content
    )  # the excerpt is centred on the edit
    assert len(result.content.encode()) <= 4 * 1024
    unchanged = await ctx.sandbox.read_bytes(ctx.handle, "big.txt")
    assert unchanged == changed  # the conflict wrote nothing


async def test_the_model_can_retry_from_a_conflict_with_the_hash_it_was_given():
    ctx, _ = await make_context()
    await edit(ctx, "src/util.py", b"X = 1\nY = 2\n", UTIL)
    stale = await call(ctx, "apply_patch", edits=[e("src/util.py", UTIL, "X = 1", "X = 10")])
    fresh = stale.data["conflicts"][0]["actual_hash"]
    again = await call(ctx, "apply_patch", edits=[e("src/util.py", fresh, "X = 1", "X = 10")])
    assert again.status == "ok" and await text(ctx, "src/util.py") == "X = 10\nY = 2\n"


async def test_a_conflict_redacts_secrets_in_the_excerpt():
    ctx, _ = await make_context()
    await edit(ctx, "src/util.py", f'KEY = "{AWS}"\n'.encode(), UTIL)
    result = await call(ctx, "apply_patch", edits=[e("src/util.py", UTIL, "X = 1", "X = 2")])
    assert (
        result.error_code == "workspace_conflict"
        and AWS not in result.model_dump_json()
        and result.redactions == 1
    )


async def test_a_conflict_in_one_file_of_several_applies_nothing():
    ctx, _ = await make_context()
    await edit(ctx, "src/util.py", b"X = 5\n", UTIL)
    before = await ctx.sandbox.file_hashes(ctx.handle)
    result = await call(
        ctx,
        "apply_patch",
        edits=[
            e("src/app.py", APP, "return 1", "return 7"),
            e("src/util.py", UTIL, "X = 1", "X = 70"),
        ],
    )
    assert result.status == "invalid" and result.error_code == "workspace_conflict"
    assert await ctx.sandbox.file_hashes(ctx.handle) == before


async def test_applying_a_patch_twice_is_a_no_op():
    ctx, _ = await make_context()
    patch = {
        "edits": [
            e("src/app.py", APP, "return 1", "return 7"),
            e("src/util.py", UTIL, "X = 1", "X = 70"),
        ]
    }
    first = await call(ctx, "apply_patch", **patch)
    after_first = await ctx.sandbox.file_hashes(ctx.handle)
    second = await call(ctx, "apply_patch", **patch)
    assert (
        first.data["replayed"] is False
        and second.data["replayed"] is True
        and second.data["revision"] == 1
    )
    assert second.status == "ok" and "No change: these edits were already applied" in second.content
    assert await ctx.sandbox.file_hashes(ctx.handle) == after_first
    assert ctx.workspace.revision == 1


async def test_a_plan_found_written_but_never_recorded_is_reported_applied_not_rewritten():
    """A crash after the sandbox write and before the record: applying the saved plan again finds it done."""
    from app.services.agent.tools.edit_tools import ApplyPatchArgs

    from tests.agent.tools.support import registry

    ctx, _ = await make_context()
    tool = registry().tool("apply_patch")
    plan = await tool.prepare(
        ctx,
        ApplyPatchArgs.model_validate({"edits": [e("src/app.py", APP, "return 1", "return 7")]}),
    )
    await ctx.sandbox.write_files(ctx.handle, plan.changes)  # written, but nothing recorded
    result = await tool.apply(ctx, plan)
    assert result.status == "ok" and result.data["replayed"] is True and ctx.workspace.revision == 0
    assert "No change: these edits were already applied" in result.content


async def test_a_patch_overtaken_by_later_edits_is_a_conflict_not_a_silent_second_application():
    ctx, _ = await make_context()
    patch = {"edits": [e("src/app.py", APP, "return 1", "return 7")]}
    await call(ctx, "apply_patch", **patch)
    await edit(
        ctx,
        "src/app.py",
        b"def main():\n    return 99\n",
        sha(await ctx.sandbox.read_bytes(ctx.handle, "src/app.py")),
    )
    again = await call(ctx, "apply_patch", **patch)
    assert again.status == "invalid" and again.error_code == "workspace_conflict"


async def test_prepare_plans_the_edit_with_hunks_without_writing_anything():
    from app.services.agent.tools.edit_tools import ApplyPatchArgs

    from tests.agent.tools.support import registry

    ctx, _ = await make_context()
    tool = registry().tool("apply_patch")
    args = ApplyPatchArgs.model_validate({"edits": [e("src/app.py", APP, "return 1", "return 7")]})
    plan = await tool.prepare(ctx, args)
    assert [f.path for f in plan.files] == ["src/app.py"] and plan.files[0].change == "modify"
    [hunk] = plan.files[0].hunks
    assert (
        hunk.hunk_id == "h1"
        and hunk.old_start >= 1
        and "-    return 1" in hunk.preview
        and "+    return 7" in hunk.preview
    )
    assert plan.summary.startswith("1 file (src/app.py): +1 -1") and "@@" in plan.preview
    assert await ctx.sandbox.file_hashes(ctx.handle) == {
        p: sha(d) for p, d in FILES.items()
    }  # nothing written
    applied = await tool.apply(ctx, plan)  # approving applies exactly the plan
    assert applied.status == "ok" and ctx.workspace.revision == 1


async def test_the_diff_preview_never_shows_a_secret():
    ctx, _ = await make_context()
    result = await call(ctx, "apply_patch", edits=[e("src/util.py", UTIL, "X = 1", f'X = "{AWS}"')])
    assert (
        result.status == "ok"
        and AWS not in result.model_dump_json()
        and "[REDACTED_SECRET:aws_access_key]" in result.content
    )
    assert AWS in await text(
        ctx, "src/util.py"
    )  # the file itself is the user's; only what the model sees is redacted


async def test_patch_rejections():
    ctx, _ = await make_context()
    assert (await call(ctx, "apply_patch", edits=[e("../x", APP, "a", "b")])).status == "denied"
    assert (
        await call(ctx, "apply_patch", edits=[e(".env", sha(FILES[".env"]), "SECRET", "X")])
    ).error_code == "sensitive_path"
    assert (
        "no such file"
        in (await call(ctx, "apply_patch", edits=[e("nope.py", APP, "a", "b")])).content
    )
    assert (
        await call(
            ctx, "apply_patch", edits=[e("data/blob.bin", sha(FILES["data/blob.bin"]), "a", "b")]
        )
    ).error_code == "patch_failed"
    assert (
        await call(
            ctx,
            "apply_patch",
            edits=[e("src/app.py", APP, "a", "b"), e("src/app.py", UTIL, "c", "d")],
        )
    ).error_code == "inconsistent_hash"
    assert (await call(ctx, "apply_patch", edits=[])).error_code == "invalid_arguments"
    assert (
        await call(ctx, "apply_patch", edits=[e("src/app.py", "short", "a", "b")])
    ).error_code == "invalid_arguments"
    assert (
        await call(ctx, "apply_patch", edits=[e("src/app.py", APP, "", "b")])
    ).error_code == "invalid_arguments"
    assert (
        await call(ctx, "apply_patch", edits=[e("src/app.py", APP, "a", "b")] * 51)
    ).error_code == "invalid_arguments"
    too_big = await call(
        ctx,
        "apply_patch",
        edits=[e("src/app.py", APP, "x" * 150_000, "y" * 150_000) for _ in range(4)],
    )
    assert too_big.error_code == "patch_too_large"


async def test_without_a_workspace_the_edit_tools_are_unavailable():
    ctx, _ = await make_context(workspace=False)
    result = await call(ctx, "apply_patch", edits=[e("src/app.py", APP, "return 1", "return 7")])
    assert result.status == "unavailable" and result.error_code == "no_workspace"
