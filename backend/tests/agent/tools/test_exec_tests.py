# ruff: noqa: E501
"""run_tests: runner detection, structured results for pytest and vitest fixtures, and the test.result event."""

import hashlib
import json
from pathlib import Path

import pytest
from app.adapters.sandbox.fake import ScriptedExec
from app.contracts.agent_events import validate_payload
from app.services.agent.tools.runners import REPORT_MARKER, event_payload

from tests.agent.tools.support import AWS, call, make_context

FIXTURES = Path(__file__).parent / "fixtures"
PYTEST_XML = (FIXTURES / "pytest-junit.xml").read_text()
VITEST_XML = (FIXTURES / "vitest-junit.xml").read_text()

PY = {
    "pyproject.toml": b"[tool.pytest.ini_options]\n",
    "tests/test_auth.py": b"def test_x(): pass\n",
}
JS = {
    "package.json": b'{"devDependencies": {"vitest": "^1"}}',
    "src/math.test.ts": b"test('a', () => {})\n",
}


def framed(output: str, report: str) -> str:
    return f"{output}\n\n{REPORT_MARKER}\n{report}\n"


async def run_tests(files, stdout, exit_code=1, **args):
    ctx, sandbox = await make_context(files=files)
    sandbox.script(
        ("sh",) if "sh" in str(args.pop("_sh", "sh")) else ("go",),
        ScriptedExec(stdout=stdout, exit_code=exit_code),
    )
    return ctx, sandbox, await call(ctx, "run_tests", **args)


async def test_pytest_is_detected_and_its_junit_becomes_a_structured_result():
    _, sandbox, result = await run_tests(
        PY, framed("F.s.. [100%]\n2 failed, 2 passed, 1 skipped in 0.41s", PYTEST_XML)
    )
    assert (
        result.status == "ok"
        and result.data["runner"] == "pytest"
        and result.data["parsed"] is True
        and result.data["exit_code"] == 1
    )
    assert (result.tests.passed, result.tests.failed, result.tests.skipped) == (2, 2, 1)
    lines = result.content.splitlines()
    assert lines[1] == "pytest: 2 passed, 2 failed, 1 skipped" and lines[2].startswith(
        "exit code 1 in "
    )
    assert (
        "tests.test_auth.TestToken::test_expiry  (tests/test_auth.py)\n    assert 1 == 2"
        in result.content
    )
    assert (
        "tests/test_auth.py:71" in result.content
        and "--- output ---\nF.s.. [100%]" in result.content
    )
    assert (
        REPORT_MARKER not in result.content and "<testsuites" not in result.content
    )  # the report is not shown twice
    argv = sandbox.exec_calls[-1].argv
    assert (
        argv[:2] == ("sh", "-c")
        and "--junitxml=/tmp/codespy-report" in argv[2]
        and argv[3] == "runner"
    )


async def test_vitest_is_detected_from_package_json_and_its_junit_is_read():
    _, sandbox, result = await run_tests(JS, framed("Test Files  1 failed", VITEST_XML))
    assert result.data["runner"] == "vitest" and (
        result.tests.passed,
        result.tests.failed,
        result.tests.skipped,
    ) == (2, 1, 1)
    assert (
        "multiplies > adds  (src/math.test.ts)" in result.content
        and "expected 6 to be 7" in result.content
    )
    assert "--reporter=junit" in sandbox.exec_calls[-1].argv[2]


async def test_the_test_result_event_payload_is_valid_for_the_pytest_and_vitest_fixtures():
    for files, xml, runner, counts in (
        (PY, PYTEST_XML, "pytest", (2, 2, 1)),
        (JS, VITEST_XML, "vitest", (2, 1, 1)),
    ):
        _, _, result = await run_tests(files, framed("out", xml))
        payload = event_payload("tc_9", result.tests)
        canonical = validate_payload("test.result", payload)  # the protocol accepts it as is
        assert (
            canonical["runner"] == runner
            and (canonical["passed"], canonical["failed"], canonical["skipped"]) == counts
        )
        assert canonical["tool_call_id"] == "tc_9" and 1 <= len(canonical["failures"]) <= 50


async def test_go_and_cargo_are_read_from_their_own_output():
    ctx, sandbox = await make_context(files={"go.mod": b"module m\n", "main.go": b"package main\n"})
    sandbox.script(
        ("go",), ScriptedExec(stdout=(FIXTURES / "go-test.jsonl").read_text(), exit_code=1)
    )
    go = await call(ctx, "run_tests")
    assert go.data["runner"] == "go" and (go.tests.passed, go.tests.failed, go.tests.skipped) == (
        1,
        1,
        1,
    )
    assert sandbox.exec_calls[-1].argv == ("go", "test", "-json", "./...")
    ctx, sandbox = await make_context(files={"Cargo.toml": b"[package]\n"})
    sandbox.script(
        ("cargo",), ScriptedExec(stdout=(FIXTURES / "cargo-test.txt").read_text(), exit_code=101)
    )
    cargo = await call(ctx, "run_tests", selector="tests::subs")
    assert cargo.tests.failed == 1 and sandbox.exec_calls[-1].argv == (
        "cargo",
        "test",
        "--",
        "tests::subs",
    )


async def test_a_named_runner_overrides_detection_and_an_unknown_one_is_invalid():
    ctx, sandbox = await make_context(files=PY | JS)
    sandbox.script(("sh",), ScriptedExec(stdout=framed("x", VITEST_XML)))
    assert (await call(ctx, "run_tests", runner="vitest")).data["runner"] == "vitest"
    assert (await call(ctx, "run_tests")).data[
        "runner"
    ] == "pytest"  # python wins when both are present
    assert (await call(ctx, "run_tests", runner="mocha")).error_code == "invalid_arguments"


async def test_without_a_detectable_runner_the_tool_says_what_it_looked_for():
    ctx, sandbox = await make_context(files={"README.md": b"# x\n"})
    result = await call(ctx, "run_tests")
    assert (
        result.status == "invalid"
        and result.error_code == "no_runner"
        and "pytest, vitest, jest, go and cargo" in result.content
    )
    assert sandbox.exec_calls == []


@pytest.mark.parametrize(
    "bad",
    [{"selector": "-rf"}, {"keyword": "--collect-only"}, {"selector": "a\x00b"}, {"selector": ""}],
)
async def test_a_selector_or_keyword_can_never_become_a_flag(bad):
    ctx, sandbox = await make_context(files=PY)
    assert (
        await call(ctx, "run_tests", **bad)
    ).error_code == "invalid_arguments" and sandbox.exec_calls == []


async def test_selector_and_keyword_are_passed_as_separate_arguments_never_spliced_into_a_script():
    ctx, sandbox = await make_context(files=PY)
    sandbox.script(("sh",), ScriptedExec(stdout=framed("", PYTEST_XML)))
    await call(ctx, "run_tests", selector="tests/test_auth.py::test_x; rm -rf /", keyword="a'b")
    argv = sandbox.exec_calls[-1].argv
    assert (
        argv[3:] == ("runner", "-k", "a'b", "tests/test_auth.py::test_x; rm -rf /")
        and "rm -rf" not in argv[2]
    )


async def test_an_unreadable_report_keeps_the_output_and_says_so():
    _, _, result = await run_tests(PY, framed("collected 3 items\nboom", "<not-xml"), exit_code=2)
    assert result.status == "ok" and result.tests is None and result.data["parsed"] is False
    assert "report could not be read" in result.content and "collected 3 items" in result.content


async def test_a_nonzero_exit_with_no_failed_test_warns_that_the_run_may_not_have_started():
    empty = '<testsuites><testsuite tests="1"><testcase classname="t" name="ok"/></testsuite></testsuites>'
    _, _, result = await run_tests(PY, framed("ImportError", empty), exit_code=2)
    assert (
        result.tests.failed == 0
        and "exit code 2 with no failed test: the run may not have started" in result.content
    )


async def test_a_timeout_has_no_summary_and_is_reported_as_one(monkeypatch):
    ctx, sandbox = await make_context(files=PY)
    sandbox.script(("sh",), ScriptedExec(delay_s=30))
    result = await call(ctx, "run_tests", timeout_s=1)
    assert (
        result.status == "timed_out"
        and result.tests is None
        and "timed out after 1 s" in result.content
    )


async def test_the_report_is_not_streamed_and_failure_text_is_redacted():
    xml = PYTEST_XML.replace("assert 1 == 2", f"key {AWS}")
    ctx, sandbox = await make_context(files=PY)
    sandbox.script(("sh",), ScriptedExec(stdout=framed("progress " * 400, xml), exit_code=1))
    streamed: list[str] = []

    async def sink(batch):
        streamed.append(batch.text)

    ctx = ctx.__class__(
        sandbox=ctx.sandbox, handle=ctx.handle, workspace=ctx.workspace, on_output=sink
    )
    result = await call(ctx, "run_tests")
    live = "".join(streamed)
    assert "progress" in live and "<testsuites" not in live and REPORT_MARKER not in live
    assert AWS not in result.model_dump_json() and result.redactions >= 1


async def test_a_failing_run_is_data_not_an_error_and_the_summary_round_trips():
    _, _, result = await run_tests(PY, framed("", PYTEST_XML))
    assert type(result).model_validate_json(result.model_dump_json()) == result
    assert (
        json.loads(result.model_dump_json())["tests"]["failed"] == 2 and hashlib
    )  # serialisable for the event stream


async def test_run_tests_runs_in_the_requested_directory_for_monorepos():
    ctx, sandbox = await make_context(
        files={"web/package.json": b'{"devDependencies": {"jest": "1"}}', "web/a.test.js": b"x\n"}
    )
    sandbox.script(("sh",), ScriptedExec(stdout=framed("", "{}")))
    result = await call(ctx, "run_tests", cwd="web")
    assert result.data["runner"] == "jest" and sandbox.exec_calls[-1].cwd == "web"
