# ruff: noqa: E501, S604
"""run_command and run_tests in real containers: real exit codes, timeouts, output limits and a real report."""

import hashlib
import json
from uuid import uuid4

import pytest
from app.contracts.workspace import SandboxSpecV1, SnapshotRefV1
from app.services.agent.tools import tools_for_mode
from app.services.agent.tools.base import ToolContext
from app.services.workspace.local import LocalWorkspace

from tests.agent.sandbox.docker_support import IMAGE, Harness, requires_docker

pytestmark = [pytest.mark.sandbox, requires_docker]

SNAP = uuid4()
# A stand-in for pytest that speaks its command line and writes a JUnit report, so the whole path
# (script, report file in the container, framing, parsing) runs for real without installing anything.
FAKE_PYTEST = b"""
import sys
args = sys.argv[1:]
path = next(a.split("=", 1)[1] for a in args if a.startswith("--junitxml="))
keyword = args[args.index("-k") + 1] if "-k" in args else ""
cases = [("tests.test_a", "test_ok", None), ("tests.test_a", "test_bad", "assert 1 == 2"), ("tests.test_b", "test_other", None)]
cases = [c for c in cases if keyword in c[1]]
body = "".join(
    f'<testcase classname="{c}" name="{n}" time="0.001">' + (f'<failure message="{m}">trace</failure>' if m else "") + "</testcase>"
    for c, n, m in cases
)
failed = sum(1 for *_, m in cases if m)
open(path, "w").write(f'<testsuites><testsuite name="pytest" tests="{len(cases)}" failures="{failed}" time="0.05">{body}</testsuite></testsuites>')
print(f"selected {len(cases)} tests; args: {' '.join(args)}")
sys.exit(1 if failed else 0)
"""
FILES = {
    "pyproject.toml": b"[tool.pytest.ini_options]\n",
    "pytest/__init__.py": b"",
    "pytest/__main__.py": FAKE_PYTEST,
    "src/hello.py": b"print('hello')\n",
}


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
    workspace = LocalWorkspace(harness.sandbox, handle)
    await workspace.baseline()
    yield ToolContext(sandbox=harness.sandbox, handle=handle, workspace=workspace)
    await harness.close()


async def call(ctx, name, **args):
    return await tools_for_mode("act").execute(ctx, name, json.dumps(args))


@pytest.mark.parametrize("code", [0, 1, 2, 7, 126, 127, 255])
async def test_real_exit_codes_are_reported_exactly(ctx, code):
    result = await call(
        ctx, "run_command", argv=["sh", "-c", f"echo out; echo err >&2; exit {code}"]
    )
    assert result.status == "ok" and result.data["exit_code"] == code
    assert (
        result.content.splitlines()[1].startswith(f"exit code {code} in ")
        and "--- stdout ---\nout\n--- stderr ---\nerr" in result.content
    )


async def test_a_missing_program_is_exit_127_from_the_container_not_a_tool_error(ctx):
    result = await call(ctx, "run_command", argv=["no-such-program"])
    assert result.status == "ok" and result.data["exit_code"] in {126, 127}


async def test_a_real_timeout_stops_the_command_and_reports_the_limit(ctx):
    result = await call(ctx, "run_command", argv=["sleep", "60"], timeout_s=2)
    assert (
        result.status == "timed_out"
        and result.data["exit_code"] is None
        and result.data["timed_out"] is True
    )
    assert "timed out after 2 s" in result.content and 1500 <= result.data["duration_ms"] < 15000
    again = await call(ctx, "run_command", argv=["echo", "still alive"])
    assert again.status == "ok" and "still alive" in again.content  # the sandbox is unharmed


async def test_real_output_over_four_mebibytes_is_cut_and_reported(ctx):
    result = await call(
        ctx, "run_command", shell="head -c 5242880 /dev/zero | tr '\\0' x", timeout_s=60
    )
    assert (
        result.status == "partial"
        and result.data["truncated"] is True
        and result.data["output_bytes"] == 4 * 1024 * 1024
    )
    assert (
        "output was cut at the sandbox limit (4 MiB)" in result.content
        and len(result.content.encode()) <= 32 * 1024
    )
    assert result.content.rstrip().endswith("x") and result.data["exit_code"] == 0


async def test_the_environment_and_working_directory_reach_the_command(ctx):
    result = await call(
        ctx,
        "run_command",
        argv=["sh", "-c", "echo $GREETING from $(pwd)"],
        cwd="src",
        env={"GREETING": "hi"},
    )
    assert result.status == "ok" and "hi from /workspace/src" in result.content


async def test_files_a_real_command_writes_are_recorded_as_a_revision(ctx):
    result = await call(
        ctx, "run_command", shell="mkdir -p out && echo built > out/a.txt && echo more > out/b.txt"
    )
    assert (
        "workspace changed by the command: 2 files (revision 1): out/a.txt, out/b.txt"
        in result.content
    )
    assert (await ctx.sandbox.read_bytes(ctx.handle, "out/a.txt")) == b"built\n"
    assert (
        "workspace changed"
        not in (await call(ctx, "run_command", argv=["cat", "out/a.txt"])).content
    )


async def test_refused_commands_never_start_a_container(ctx):
    result = await call(ctx, "run_command", argv=["rm", "-rf", "/workspace"])
    assert (
        result.status == "denied"
        and (await ctx.sandbox.read_file(ctx.handle, "src/hello.py")).content == "print('hello')\n"
    )


async def test_run_tests_runs_a_real_runner_and_reads_its_real_report(ctx):
    result = await call(ctx, "run_tests")
    assert (
        result.status == "ok"
        and result.data["runner"] == "pytest"
        and result.data["parsed"] is True
        and result.data["exit_code"] == 1
    )
    assert (result.tests.passed, result.tests.failed) == (2, 1)
    assert (
        "pytest: 2 passed, 1 failed, 0 skipped" in result.content
        and "tests.test_a::test_bad" in result.content
        and "assert 1 == 2" in result.content
    )
    assert "selected 3 tests" in result.content and "<testsuites" not in result.content
    assert (
        "workspace changed" not in result.content
    )  # the report lives in the container's temp space, not the workspace


async def test_run_tests_passes_selector_and_keyword_as_real_arguments(ctx):
    result = await call(ctx, "run_tests", keyword="ok", selector="tests/test_a.py")
    assert result.tests.failed == 0 and result.data["exit_code"] == 0
    assert "args: " in result.content and "-k ok tests/test_a.py" in result.content


async def test_a_selector_that_looks_like_shell_is_inert(ctx):
    result = await call(ctx, "run_tests", selector="x; touch /workspace/pwned #")
    assert (
        result.status == "ok"
        and "pwned" not in await ctx.sandbox.file_hashes(ctx.handle)
        and hashlib
    )
    assert not any(p.endswith("pwned") for p in await ctx.sandbox.file_hashes(ctx.handle))
