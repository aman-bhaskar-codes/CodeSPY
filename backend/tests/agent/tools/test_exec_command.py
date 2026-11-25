# ruff: noqa: E501, S604
"""run_command: exit codes, timeouts and output limits reported exactly; refusal, risk, streaming and workspace changes."""

import json

import pytest
from app.adapters.sandbox.fake import ScriptedExec
from app.services.agent.tools import tools_for_mode
from app.services.agent.tools.exec_tools import RunCommand, RunCommandArgs, classify, refuse
from app.services.agent.tools.output import OutputBatch, OutputCoalescer

from tests.agent.tools.support import AWS, FILES, call, make_context


async def run(ctx, **args):
    return await call(ctx, "run_command", **args)


@pytest.mark.parametrize("code", [0, 1, 2, 3, 126, 127, 137, 255])
async def test_every_exit_code_is_reported_exactly_in_the_first_line_and_in_the_data(code):
    ctx, sandbox = await make_context()
    sandbox.script(("prog",), ScriptedExec(stdout="out\n", exit_code=code))
    result = await run(ctx, argv=["prog", "a"])
    assert result.status == "ok" and result.data["exit_code"] == code
    lines = result.content.splitlines()
    assert lines[0] == "$ prog a" and lines[1].startswith(f"exit code {code} in ")
    assert lines[-1] == "out" and result.data["timed_out"] is False


async def test_a_failing_command_is_still_a_successful_tool_call():
    ctx, sandbox = await make_context()
    sandbox.script(("make",), ScriptedExec(stderr="make: *** No rule\n", exit_code=2))
    result = await run(ctx, argv=["make", "build"])
    assert (
        result.status == "ok"
        and result.error_code is None
        and result.data["exit_code"] == 2
        and "No rule" in result.content
    )


async def test_a_timeout_is_reported_with_its_limit_and_no_exit_code(monkeypatch):
    ctx, sandbox = await make_context()
    sandbox.script(("sleep",), ScriptedExec(delay_s=30, exit_code=0))
    result = await run(ctx, argv=["sleep", "30"], timeout_s=1)
    assert result.status == "timed_out" and result.error_code == "timed_out"
    assert "timed out after 1 s (the command was stopped and has no exit code)" in result.content
    assert (
        result.data["exit_code"] is None
        and result.data["timed_out"] is True
        and result.data["duration_ms"] >= 900
    )


async def test_output_is_shown_whole_when_short_and_by_stream_when_both_are_present():
    ctx, sandbox = await make_context()
    sandbox.script(("p",), ScriptedExec(stdout="to out\n", stderr="to err\n"))
    both = (await run(ctx, argv=["p"])).content
    assert "--- stdout ---\nto out\n--- stderr ---\nto err" in both
    sandbox.script(("q",), ScriptedExec())
    assert (await run(ctx, argv=["q"])).content.endswith("(no output)")


async def test_the_sandbox_output_cap_is_reported_as_truncation():
    ctx, sandbox = await make_context()
    sandbox.script(("big",), ScriptedExec(stdout="x" * (5 * 1024 * 1024)))
    result = await run(ctx, argv=["big"])
    assert result.status == "partial" and result.data["truncated"] is True
    assert "output was cut at the sandbox limit (4 MiB)" in result.content
    assert result.data["output_bytes"] == 4 * 1024 * 1024  # exactly what the sandbox kept
    assert len(result.content.encode()) <= 32 * 1024


async def test_the_model_sees_the_head_and_the_tail_of_long_output():
    ctx, sandbox = await make_context()
    lines = "".join(f"line {i}\n" for i in range(1, 20001))
    sandbox.script(("log",), ScriptedExec(stdout=lines))
    result = await run(ctx, argv=["log"])
    assert (
        result.status == "partial"
        and result.truncated
        and len(result.content.encode()) <= 32 * 1024
    )
    assert (
        "line 1\n" in result.content
        and result.content.rstrip().endswith("line 20000")
        and "bytes omitted" in result.content
    )


async def test_a_disk_limit_stop_is_reported_apart_from_a_timeout():
    ctx, sandbox = await make_context()
    sandbox.script(("fill",), ScriptedExec())
    original = sandbox.exec

    async def exceeding(handle, request):
        async for chunk in original(handle, request):
            yield (
                chunk.model_copy(update={"limit_exceeded": "disk", "exit_code": None})
                if chunk.kind == "exit"
                else chunk
            )

    sandbox.exec = exceeding
    result = await run(ctx, argv=["fill"])
    assert (
        result.status == "partial"
        and "stopped: the workspace grew past its disk limit" in result.content
    )
    assert result.data["limit_exceeded"] == "disk" and result.data["timed_out"] is False


async def test_secrets_in_output_and_arguments_are_redacted():
    ctx, sandbox = await make_context()
    sandbox.script(("env-dump",), ScriptedExec(stdout=f"KEY={AWS}\n"))
    result = await run(ctx, argv=["env-dump", f"--token={AWS}"])
    assert AWS not in result.model_dump_json() and result.redactions >= 2


@pytest.mark.parametrize(
    "argv",
    [
        ["rm", "-rf", "/"],
        ["rm", "-rf", "/*"],
        ["rm", "-r", "/workspace"],
        ["rm", "-fr", "/workspace/*"],
        ["/bin/rm", "-rf", ".."],
        ["rm", "-rf", "~"],
        ["sudo", "ls"],
        ["mkfs.ext4", "/dev/sda"],
        ["shutdown", "now"],
        ["dd", "if=/dev/zero", "of=/dev/sda"],
    ],
)
async def test_destructive_commands_are_refused_before_running(argv):
    ctx, sandbox = await make_context()
    result = await run(ctx, argv=argv)
    assert (
        result.status == "denied"
        and result.error_code == "command_refused"
        and sandbox.exec_calls == []
    )


@pytest.mark.parametrize(
    "shell",
    [
        ":(){ :|:& };:",
        "rm -rf /",
        "rm -rf ~ ",
        "dd if=/dev/zero of=/dev/sda",
        "sudo make install",
        "cat x; shutdown -h now",
    ],
)
async def test_destructive_shell_strings_are_refused_too(shell):
    ctx, sandbox = await make_context()
    assert (
        await run(ctx, shell=shell)
    ).error_code == "command_refused" and sandbox.exec_calls == []


async def test_ordinary_cleanup_is_allowed():
    ctx, sandbox = await make_context()
    sandbox.script(("rm",), ScriptedExec())
    assert (await run(ctx, argv=["rm", "-rf", "build", "dist"])).status == "ok"
    assert (await run(ctx, argv=["rm", "-rf", "./node_modules"])).status == "ok"


async def test_exactly_one_of_argv_and_shell_is_required():
    ctx, sandbox = await make_context()
    for args in ({}, {"argv": ["a"], "shell": "a"}):
        assert (await run(ctx, **args)).error_code == "invalid_arguments"
    assert sandbox.exec_calls == []


async def test_a_shell_string_runs_through_sh_and_is_at_least_medium_risk():
    ctx, sandbox = await make_context()
    sandbox.script(("sh",), ScriptedExec(stdout="3\n"))
    result = await run(ctx, shell="ls | wc -l")
    assert (
        result.status == "ok"
        and sandbox.exec_calls[-1].argv == ("sh", "-c", "ls | wc -l")
        and result.data["risk"] in {"medium", "high"}
    )


async def test_environment_and_working_directory_are_validated():
    ctx, sandbox = await make_context()
    assert (
        await run(ctx, argv=["a"], env={"LD_PRELOAD": "/x.so"})
    ).error_code == "invalid_arguments"
    assert (await run(ctx, argv=["a"], env={"bad key": "v"})).error_code == "invalid_arguments"
    for cwd in ("../x", "/etc", ".git"):
        assert (await run(ctx, argv=["a"], cwd=cwd)).status == "denied"
    missing = await run(ctx, argv=["a"], cwd="nope")
    assert missing.status == "invalid" and missing.error_code == "exec_rejected"
    assert (await run(ctx, argv=["a"], timeout_s=0)).error_code == "invalid_arguments"
    assert (await run(ctx, argv=["a"], timeout_s=601)).error_code == "invalid_arguments"
    assert sandbox.exec_calls == []
    sandbox.script(("pwd",), ScriptedExec(stdout="/workspace/src\n"))
    ok = await run(ctx, argv=["pwd"], cwd="src", env={"CI": "1"})
    assert (
        ok.status == "ok"
        and sandbox.exec_calls[-1].cwd == "src"
        and sandbox.exec_calls[-1].env == {"CI": "1"}
        and "(in src) pwd" in ok.content
    )


@pytest.mark.parametrize(
    ("argv", "risk"),
    [
        (["ls", "-la"], "low"),
        (["cat", "a"], "low"),
        (["python3", "-c", "x"], "medium"),
        (["python3", "script.py"], "low"),
        (["find", ".", "-delete"], "medium"),
        (["npm", "install"], "medium"),
        (["rm", "a"], "high"),
        (["chmod", "+x", "a"], "high"),
        (["make"], "medium"),
        (["git", "log"], "medium"),
    ],
)
def test_risk_is_a_signal_for_the_approval_card(argv, risk):
    assert classify(argv, None) == risk


def test_shell_risk_and_refusal_functions():
    assert (
        classify((), "ls") == "medium"
        and classify((), "rm a && ls") == "high"
        and classify((), "ls | wc") == "high"
    )
    assert refuse(["ls"], None) is None and refuse((), "make test") is None


async def test_prepare_returns_the_exact_command_for_approval_without_running_it():
    ctx, sandbox = await make_context()
    plan = await RunCommand().prepare(
        ctx, RunCommandArgs(argv=["npm", "test", "--", "-t", "a b"], cwd="src", timeout_s=60)
    )
    assert (
        plan.display == "(in src) npm test -- -t 'a b'" and plan.rule_key == "npm test -- -t 'a b'"
    )
    assert (plan.argv, plan.cwd, plan.timeout_s, plan.risk, plan.shell) == (
        ("npm", "test", "--", "-t", "a b"),
        "src",
        60,
        "medium",
        False,
    )
    assert sandbox.exec_calls == []


async def test_files_a_command_writes_become_a_workspace_revision_and_are_named():
    ctx, sandbox = await make_context()
    sandbox.script(
        ("build",),
        ScriptedExec(stdout="built\n", writes={"dist/out.js": b"x", "dist/map.js": b"y"}),
    )
    result = await run(ctx, argv=["build"])
    assert (
        "workspace changed by the command: 2 files (revision 1): dist/map.js, dist/out.js"
        in result.content
    )
    assert result.data["workspace_revision"] == 1 and result.data["files_changed"] == 2
    again = await run(ctx, argv=["build"])  # the same files, the same content: nothing new
    assert "workspace changed" not in again.content and ctx.workspace.revision == 1
    sandbox.script(("clean",), ScriptedExec())
    assert "workspace changed" not in (await run(ctx, argv=["clean"])).content


async def test_a_command_is_never_run_twice_by_one_call():
    ctx, sandbox = await make_context()
    sandbox.script(("once",), ScriptedExec())
    await run(ctx, argv=["once"])
    assert [c.argv for c in sandbox.exec_calls] == [("once",)]


async def test_output_streams_as_coalesced_batches_with_offsets():
    ctx, sandbox = await make_context()
    sandbox.script(("chat",), ScriptedExec(stdout="a" * 3000 + "\n", stderr="warn\n"))
    seen: list[OutputBatch] = []

    async def sink(batch):
        seen.append(batch)

    ctx = ctx.__class__(
        sandbox=ctx.sandbox, handle=ctx.handle, workspace=ctx.workspace, on_output=sink
    )
    await run(ctx, argv=["chat"])
    assert [b.stream for b in seen] == ["stdout", "stderr"] and "".join(
        b.text for b in seen if b.stream == "stdout"
    ) == "a" * 3000 + "\n"
    assert seen[0].offset == 0 and seen[1].offset == 0 and all(len(b.text) <= 4096 for b in seen)


async def test_the_coalescer_flushes_by_size_age_and_stream_and_numbers_offsets_in_utf16_units():
    now = [0.0]
    batches: list[OutputBatch] = []

    async def sink(b):
        batches.append(b)

    co = OutputCoalescer(sink, clock=lambda: now[0])
    await co.feed("stdout", "ab")
    await co.feed("stdout", "cd")
    assert batches == []  # small and recent: held
    now[0] = 0.25
    await co.feed("stdout", "ef")  # 200 ms have passed
    assert [(b.stream, b.offset, b.text) for b in batches] == [("stdout", 0, "abcdef")]
    await co.feed("stdout", "😀")
    await co.feed("stderr", "E")  # a stream switch flushes the other stream
    await co.close()
    assert [(b.stream, b.offset, b.text) for b in batches[1:]] == [
        ("stdout", 6, "😀"),
        ("stderr", 0, "E"),
    ]
    await co.feed("stdout", "x" * 1024)
    assert (
        batches[-1].offset == 8 and batches[-1].text == "x" * 1024
    )  # the emoji counted as two units


async def test_the_coalescer_caps_live_output_and_says_so():
    batches: list[OutputBatch] = []

    async def sink(b):
        batches.append(b)

    co = OutputCoalescer(sink, max_live_bytes=3000)
    for _ in range(10):
        await co.feed("stdout", "z" * 1000)
    await co.close()
    text = "".join(b.text for b in batches)
    assert len(text) < 5000 and text.endswith(
        "[live output stopped here; the full result follows]\n"
    )


async def test_flush_due_sends_quiet_output_without_waiting_for_more():
    now = [0.0]
    batches: list[OutputBatch] = []

    async def sink(b):
        batches.append(b)

    co = OutputCoalescer(sink, clock=lambda: now[0])
    await co.feed("stdout", "tail -f")
    await co.flush_due()
    assert batches == []
    now[0] = 0.3
    await co.flush_due()
    assert [b.text for b in batches] == ["tail -f"]


def test_run_command_is_an_exec_class_tool_in_act_mode_only():
    assert {"run_command", "run_tests"} <= {s.name for s in tools_for_mode("act").specs()}
    assert "run_command" not in {s.name for s in tools_for_mode("plan").specs()}
    assert RunCommand().spec().tool_class == "exec"
    schema = json.dumps(RunCommand().spec().parameters_schema)
    assert "argv" in schema and "timeout_s" in schema
    assert FILES  # the shared fixture repository is in scope for every test above
