"""list_dir, read_file, glob and grep over the sandbox workspace."""

import pytest

from tests.agent.tools.support import CANARY, FILES, SECRETS, call, grep_output, make_context


async def test_list_dir_shows_directories_files_and_sizes():
    ctx, _ = await make_context()
    result = await call(ctx, "list_dir")
    assert result.status == "ok" and result.summary.startswith(".: ")
    assert "src/  (3 files)" in result.content and "README.md  7 B" in result.content
    deep = await call(ctx, "list_dir", path="src", depth=1)
    assert "app.py" in deep.content and "util.py" in deep.content and "README" not in deep.content
    two = await call(ctx, "list_dir", depth=2)
    assert "  app.py" in two.content  # nested entries are indented under their directory


async def test_list_dir_marks_credential_files_without_reading_them():
    ctx, sandbox = await make_context()
    result = await call(ctx, "list_dir")
    assert ".env" in result.content and "content withheld" in result.content
    assert sandbox.reads == []  # a listing never opens a file


async def test_list_dir_rejects_files_and_missing_directories_gracefully():
    ctx, _ = await make_context()
    assert (await call(ctx, "list_dir", path="README.md")).error_code == "not_a_directory"
    assert "no such directory" in (await call(ctx, "list_dir", path="nope")).content


async def test_read_file_numbers_lines_and_names_the_hash_the_edit_tools_need():
    ctx, _ = await make_context()
    result = await call(ctx, "read_file", path="src/app.py")
    assert result.status == "ok"
    assert "src/app.py (lines 1-6 of 6; sha256 " in result.content
    assert "     1\tdef main():" in result.content and "     6\t    return 1" in result.content
    assert result.data["file_hash"] and result.data["total_lines"] == 6
    [evidence] = result.evidence
    assert evidence.provenance.kind == "source_range" and (
        evidence.provenance.start_line,
        evidence.provenance.end_line,
    ) == (1, 6)


async def test_read_file_ranges_and_the_800_line_ceiling():
    ctx, _ = await make_context()
    part = await call(ctx, "read_file", path="big.txt", start_line=10, end_line=12)
    assert (
        "    10\tline 10" in part.content
        and "    12\tline 12" in part.content
        and "line 13" not in part.content
    )
    wide = await call(ctx, "read_file", path="big.txt", start_line=1, end_line=2000)
    assert (
        wide.status == "partial"
        and "   800\tline 800" in wide.content
        and "line 801" not in wide.content
    )
    default = await call(ctx, "read_file", path="big.txt")
    assert default.status == "partial" and "line 800" in default.content
    empty = await call(ctx, "read_file", path="src/util.py", start_line=50)
    assert empty.status == "ok" and "no lines in that range" in empty.content


async def test_read_file_output_is_bounded_with_head_and_tail_kept():
    long_lines = {"wide.txt": ("x" * 400 + "\n").encode() * 200}
    ctx, _ = await make_context(files=long_lines)
    result = await call(ctx, "read_file", path="wide.txt")
    assert result.status == "partial" and result.truncated
    assert len(result.content.encode()) <= 32 * 1024 and result.bytes_total > 32 * 1024
    assert "bytes omitted" in result.content
    assert result.content.splitlines()[1].startswith(
        "     1\t"
    ) and result.content.rstrip().splitlines()[-1].startswith("   200\t")


async def test_read_file_reports_errors_as_results():
    ctx, _ = await make_context()
    assert (await call(ctx, "read_file", path="nope.txt")).error_code == "file_not_found"
    assert (await call(ctx, "read_file", path="src")).error_code == "not_text"
    assert (await call(ctx, "read_file", path="data/blob.bin")).error_code == "not_text"
    assert (
        await call(ctx, "read_file", path="big.txt", start_line=9, end_line=3)
    ).status == "denied"


async def test_glob_matches_paths_in_order():
    ctx, _ = await make_context()
    assert (await call(ctx, "glob", pattern="src/*.py")).content.splitlines() == [
        "src/app.py",
        "src/config.py",
        "src/util.py",
    ]
    assert (await call(ctx, "glob", pattern="**/*.md")).content.splitlines() == [
        "README.md",
        "docs/guide.md",
    ]
    none = await call(ctx, "glob", pattern="*.rs")
    assert none.status == "ok" and "0 files match" in none.content


async def test_grep_parses_matches_and_limits_to_the_glob():
    ctx, sandbox = await make_context()
    sandbox.script(
        ("grep",),
        grep_output(
            ("src/app.py", 1, "def main():"),
            ("src/util.py", 1, "X = 1"),
            ("docs/guide.md", 2, "guide"),
        ),
    )
    result = await call(ctx, "grep", pattern="main", path_glob="src/**")
    assert result.content == "src/app.py:1: def main():\nsrc/util.py:1: X = 1"
    assert result.summary == "2 matches for 'main'" and len(result.evidence) == 2
    argv = sandbox.exec_calls[-1].argv
    assert (
        argv[:3] == ("grep", "-rnIHZ", "--exclude-dir=.git")
        and "-F" in argv
        and argv[-4:] == ("-e", "main", "--", ".")
    )


async def test_grep_options_become_grep_flags_and_a_pattern_is_never_a_flag():
    ctx, sandbox = await make_context()
    sandbox.script(("grep",), grep_output())
    await call(ctx, "grep", pattern="-rf /etc/passwd", regex=True, ignore_case=True)
    argv = sandbox.exec_calls[-1].argv
    assert "-E" in argv and "-i" in argv and "-F" not in argv
    assert argv[argv.index("-e") + 1] == "-rf /etc/passwd" and argv.index("--") > argv.index("-e")


async def test_grep_stops_at_max_hits_and_reports_no_match_and_bad_patterns():
    ctx, sandbox = await make_context()
    sandbox.script(("grep",), grep_output(*[("big.txt", i, f"line {i}") for i in range(1, 60)]))
    capped = await call(ctx, "grep", pattern="line", max_hits=10)
    assert (
        capped.status == "partial"
        and capped.content.count("\n") == 10
        and "stopped at 10 matches" in capped.content
    )
    sandbox.script(("grep",), grep_output())
    assert (await call(ctx, "grep", pattern="zzz")).content == "no matches"
    from app.adapters.sandbox.fake import ScriptedExec

    sandbox.script(("grep",), ScriptedExec(stderr="grep: Unmatched ( or \\(\n", exit_code=2))
    bad = await call(ctx, "grep", pattern="(", regex=True)
    assert (
        bad.status == "invalid"
        and bad.error_code == "invalid_pattern"
        and "Unmatched" in bad.content
    )


async def test_grep_withholds_credential_files_and_redacts_matching_lines():
    ctx, sandbox = await make_context()
    sandbox.script(
        ("grep",),
        grep_output(
            (".env", 1, "SECRET_KEY=AKIAIOSFODNN7EXAMPLE"),
            ("src/config.py", 5, f"marker = {CANARY}"),
            ("certs/server.pem", 1, "-----BEGIN RSA PRIVATE KEY-----"),
        ),
    )
    result = await call(ctx, "grep", pattern="KEY")
    assert ".env" not in result.content and "server.pem" not in result.content
    assert (
        "2 matches in credential files were withheld" in result.content
        or "withheld" in result.content
    )
    assert (
        CANARY not in result.content
        and result.redactions == 1
        and "[REDACTED_SECRET:canary]" in result.content
    )


async def test_grep_timeout_is_reported_not_raised(monkeypatch):
    from app.services.agent.tools import workspace_tools

    monkeypatch.setattr(workspace_tools, "GREP_SECONDS", 1)
    from app.adapters.sandbox.fake import ScriptedExec

    ctx, sandbox = await make_context()
    sandbox.script(("grep",), ScriptedExec(delay_s=30))
    result = await call(ctx, "grep", pattern="x")
    assert result.status == "timed_out" and result.error_code == "timed_out"


@pytest.mark.parametrize(
    "tool",
    [
        "list_dir",
        "read_file",
        "glob",
        "grep",
        "git_diff",
        "git_status",
        "find_symbol",
        "search_semantic",
    ],
)
async def test_no_tool_leaks_a_secret_in_any_field(tool):
    ctx, sandbox = await make_context()
    sandbox.script(
        ("grep",),
        grep_output(("src/config.py", 2, FILES["src/config.py"].decode().splitlines()[1])),
    )
    args = {
        "list_dir": {"path": "src"},
        "read_file": {"path": "src/config.py"},
        "glob": {"pattern": "**"},
        "grep": {"pattern": "AWS"},
        "git_diff": {},
        "git_status": {},
        "find_symbol": {"name": "x"},
        "search_semantic": {"query": "x"},
    }[tool]
    result = await call(ctx, tool, **args)
    dump = result.model_dump_json()
    assert all(secret not in dump for secret in SECRETS)
