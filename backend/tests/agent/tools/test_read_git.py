"""git_status and git_diff: this session's changes against the snapshot it started from."""

from app.contracts.workspace import FileChangeV1
from app.services.agent.tools.text import is_sensitive

from tests.agent.tools.support import AWS, FILES, call, edit, make_context


def sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


async def changed_session():
    ctx, sandbox = await make_context()
    await edit(ctx, "src/util.py", b"X = 2\nY = 3\n", sha(FILES["src/util.py"]))
    await edit(ctx, "NEW.md", b"new file\n", None)
    await ctx.sandbox.write_files(
        ctx.handle,
        (FileChangeV1(op="delete", path="README.md", expected_hash=sha(FILES["README.md"])),),
    )
    return ctx, sandbox


async def test_status_lists_added_modified_and_deleted_files_in_order():
    ctx, _ = await changed_session()
    result = await call(ctx, "git_status")
    assert result.content.splitlines() == ["A NEW.md", "D README.md", "M src/util.py"]
    assert result.summary == "3 changed files (A 1, M 1, D 1)" and result.data == {
        "A": 1,
        "M": 1,
        "D": 1,
    }


async def test_status_of_an_untouched_session_is_empty():
    ctx, _ = await make_context()
    result = await call(ctx, "git_status")
    assert result.status == "ok" and result.content == "no changes"


async def test_a_change_and_its_reversal_leave_no_status():
    ctx, _ = await make_context()
    await edit(ctx, "src/util.py", b"X = 2\n", sha(FILES["src/util.py"]))
    await edit(ctx, "src/util.py", FILES["src/util.py"], sha(b"X = 2\n"))
    assert (await call(ctx, "git_status")).content == "no changes"  # content, not history, decides


async def test_diff_is_a_unified_diff_against_the_base():
    ctx, _ = await changed_session()
    result = await call(ctx, "git_diff", paths=["src/util.py"])
    assert result.content.splitlines()[:5] == [
        "--- a/src/util.py",
        "+++ b/src/util.py",
        "@@ -1 +1,2 @@",
        "-X = 1",
        "+X = 2",
    ]
    assert "+Y = 3" in result.content and result.status == "ok"


async def test_diff_of_added_and_deleted_files_uses_dev_null():
    ctx, _ = await changed_session()
    added = (await call(ctx, "git_diff", paths=["NEW.md"])).content
    assert added.startswith("--- /dev/null\n+++ b/NEW.md\n") and "+new file" in added
    deleted = (await call(ctx, "git_diff", paths=["README.md"])).content
    assert deleted.startswith("--- a/README.md\n+++ /dev/null\n") and "-# demo" in deleted


async def test_diff_without_paths_covers_every_changed_file_and_skips_unchanged_ones():
    ctx, _ = await changed_session()
    result = await call(ctx, "git_diff")
    assert result.content.count("\n--- ") + result.content.startswith("--- ") == 3
    assert (
        await call(ctx, "git_diff", paths=["src/app.py"])
    ).content == "no changes"  # asked for an unchanged file


async def test_diff_context_lines_are_configurable():
    ctx, _ = await make_context()
    await edit(
        ctx,
        "big.txt",
        FILES["big.txt"].replace(b"line 1000\n", b"CHANGED\n"),
        sha(FILES["big.txt"]),
    )
    narrow = (await call(ctx, "git_diff", paths=["big.txt"], context=0)).content
    wide = (await call(ctx, "git_diff", paths=["big.txt"], context=5)).content
    assert narrow.count("\n line ") == 0 and wide.count("\n line ") == 10


async def test_diff_of_binary_files_says_so_and_credential_files_are_withheld():
    ctx, _ = await make_context()
    await edit(ctx, "data/blob.bin", b"\x00\x09", sha(FILES["data/blob.bin"]))
    await edit(ctx, ".env", f"SECRET_KEY={AWS}\nMORE=1\n".encode(), sha(FILES[".env"]))
    binary = await call(ctx, "git_diff", paths=["data/blob.bin"])
    assert "Binary files a/data/blob.bin and b/data/blob.bin differ" in binary.content
    secret = await call(ctx, "git_diff", paths=[".env"])
    assert "content withheld" in secret.content and AWS not in secret.content


async def test_diff_redacts_secrets_in_changed_text_and_never_leaves_the_workspace():
    ctx, _ = await make_context()
    await edit(ctx, "src/util.py", f'KEY = "{AWS}"\n'.encode(), sha(FILES["src/util.py"]))
    result = await call(ctx, "git_diff")
    assert (
        AWS not in result.content
        and result.redactions >= 1
        and "[REDACTED_SECRET:aws_access_key]" in result.content
    )
    assert (await call(ctx, "git_diff", paths=["../outside"])).status == "denied"


async def test_a_large_diff_is_bounded_with_its_ends_kept():
    ctx, _ = await make_context()
    await edit(
        ctx,
        "big.txt",
        b"".join(f"changed {i}\n".encode() for i in range(1, 6000)),
        sha(FILES["big.txt"]),
    )
    result = await call(ctx, "git_diff", paths=["big.txt"])
    assert (
        result.status == "partial"
        and result.truncated
        and len(result.content.encode()) <= 32 * 1024
    )
    assert result.content.startswith("--- a/big.txt") and result.content.rstrip().endswith(
        "+changed 5999"
    )


async def test_more_than_twenty_changed_files_are_summarised():
    ctx, _ = await make_context()
    for i in range(25):
        await edit(ctx, f"gen/f{i:02}.txt", b"x\n", None)
    result = await call(ctx, "git_diff")
    assert result.status == "partial" and "5 more changed files not shown" in result.content


async def test_without_a_base_snapshot_the_tools_say_so():
    ctx, _ = await make_context(base=False)
    for tool in ("git_status", "git_diff"):
        result = await call(ctx, tool)
        assert result.status == "unavailable" and result.error_code == "no_base_snapshot"


def test_the_sensitive_path_rule_covers_what_the_diff_withholds():
    assert (
        is_sensitive(".env") and is_sensitive("certs/server.pem") and not is_sensitive("src/app.py")
    )
