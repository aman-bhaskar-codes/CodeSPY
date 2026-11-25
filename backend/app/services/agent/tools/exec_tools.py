# ruff: noqa: E501
"""Exec tools: ``run_command`` and ``run_tests``.

A command runs in the sandbox, never in the API or worker process, and never with a shell unless
the caller asks for one and says so. Exit codes, timeouts and output limits are reported exactly:
the exit code is in the first line of the result and in its data, a timeout says how long the limit
was, and truncation says where it happened. A command is run once per call and never re-run by
the tool. Planning a command (``prepare``) checks and classifies it without running it, so the
runner can show the exact argv and ask before ``run``.
"""

import asyncio
import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import Field, ValidationError, model_validator

from app.contracts.agent_events import Risk
from app.contracts.agent_tools import AgentToolArgs, AgentToolResultV1
from app.contracts.workspace import ExecRequestV1
from app.ports.sandbox import ExecRejected, PathRejected, normalize_workspace_path
from app.services.agent.tools.base import Tool, ToolContext, fail, succeed
from app.services.agent.tools.output import OutputCoalescer
from app.services.agent.tools.runners import (
    REPORT_MARKER,
    RUNNERS,
    Runner,
    command_for,
    detect_runners,
    redact_summary,
    split_report,
    summarize,
)

MAX_SECTION_BYTES = 12 * 1024
TICK_SECONDS = 0.1

# Commands refused whatever the approval policy says: they cannot be part of building or testing code.
_DENY_ARGV = {
    "mkfs": "formatting a filesystem",
    "mkfs.ext4": "formatting a filesystem",
    "shutdown": "stopping the machine",
    "reboot": "stopping the machine",
    "halt": "stopping the machine",
    "poweroff": "stopping the machine",
    "sudo": "privilege escalation",
    "su": "privilege escalation",
}
_ROOTISH = {
    "/",
    "/*",
    "/workspace",
    "/workspace/",
    "/workspace/*",
    "~",
    "~/",
    "$HOME",
    "..",
    "../*",
    "/..",
    "/home",
    "/etc",
    "/usr",
    "/bin",
    "/lib",
}
_DENY_SHELL = re.compile(
    r"(:\(\)\s*\{|\bmkfs\b|\bdd\b[^|;&]*\bof=/dev/|>\s*/dev/sd|\b(shutdown|reboot|halt|poweroff|sudo)\b"
    r"|\brm\s+(-[a-zA-Z]*[rR][a-zA-Z]*\s+)+(--\s+)?(/|/\*|~|\$HOME|\.\.)(\s|$))"
)
_LOW_RISK = {
    "ls",
    "cat",
    "echo",
    "pwd",
    "head",
    "tail",
    "wc",
    "grep",
    "find",
    "stat",
    "file",
    "tree",
    "diff",
    "sort",
    "uniq",
    "which",
    "env",
    "date",
    "true",
    "false",
    "printf",
    "python3",
    "python",
    "node",
}
_HIGH_RISK = {
    "rm",
    "rmdir",
    "mv",
    "chmod",
    "chown",
    "kill",
    "pkill",
    "killall",
    "dd",
    "truncate",
    "shred",
}
_INSTALLERS = (
    {"pip", "pip3", "npm", "yarn", "pnpm", "apt", "apt-get", "cargo", "go", "gem", "brew"},
    {"install", "add", "i", "get", "update", "upgrade"},
)


def refuse(argv: Sequence[str], shell: str | None) -> str | None:
    """A reason this command is never run, or ``None``."""
    if shell is not None:
        if _DENY_SHELL.search(shell):
            return "that command is destructive or escalates privilege"
        return None
    program = argv[0].rsplit("/", 1)[-1]
    if program in _DENY_ARGV:
        return f"{_DENY_ARGV[program]} is not allowed"
    if program in {"rm", "rmdir"}:
        flags = [a for a in argv[1:] if a.startswith("-")]
        targets = [a for a in argv[1:] if not a.startswith("-")]
        if any("r" in f.lower() or f == "--recursive" for f in flags) and any(
            t in _ROOTISH for t in targets
        ):
            return "that would delete the workspace or system directories"
    if program == "dd" and any(a.startswith("of=/dev/") for a in argv[1:]):
        return "writing to a device is not allowed"
    return None


def classify(argv: Sequence[str], shell: str | None) -> Risk:
    """A signal for the approval card, not the gate: the policy decides what needs approval."""
    if shell is not None:
        return "high" if re.search(r"\b(rm|mv|chmod|chown|kill|dd)\b|[>|]", shell) else "medium"
    program = argv[0].rsplit("/", 1)[-1]
    if program in _HIGH_RISK:
        return "high"
    if program in _INSTALLERS[0] and any(a in _INSTALLERS[1] for a in argv[1:3]):
        return "medium"
    if program in _LOW_RISK and not any(
        a in {"-c", "-e", "--eval", "-delete", "-exec"} for a in argv[1:]
    ):
        return "low"
    return "medium"


@dataclass(frozen=True)
class ExecPlan:
    argv: tuple[str, ...]
    cwd: str
    timeout_s: int
    env: dict[str, str]
    shell: bool
    risk: Risk
    #: What the approval card shows: the exact command and where it runs.
    display: str
    #: What an approval rule matches against (``exec: "npm test*"``).
    rule_key: str
    runner: Runner | None = None


@dataclass(frozen=True)
class ExecOutcome:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    truncated: bool
    limit_exceeded: str | None
    duration_ms: int


def _display(argv: Sequence[str], cwd: str, shell: str | None) -> str:
    command = shell if shell is not None else shlex.join(argv)
    return command if cwd == "." else f"(in {cwd}) {command}"


async def execute(
    ctx: ToolContext, plan: ExecPlan, *, stop_stream_at: str | None = None
) -> ExecOutcome:
    """Run a plan once and stream its output. ``stop_stream_at`` hides what follows a marker."""
    request = ExecRequestV1(argv=plan.argv, cwd=plan.cwd, timeout_s=plan.timeout_s, env=plan.env)
    sink = OutputCoalescer(ctx.on_output) if ctx.on_output is not None else None
    ticker = asyncio.create_task(_tick(sink)) if sink is not None else None
    out: list[str] = []
    err: list[str] = []
    tail, streaming = "", True
    done = None
    try:
        async for chunk in ctx.sandbox.exec(ctx.handle, request):
            if chunk.kind == "exit":
                done = chunk
                continue
            (out if chunk.kind == "stdout" else err).append(chunk.text)
            if sink is None or not streaming:
                continue
            text = chunk.text
            if stop_stream_at and chunk.kind == "stdout":
                joined = tail + text
                at = joined.find(stop_stream_at)
                if at != -1:
                    text, streaming = joined[:at][len(tail) :] if at >= len(tail) else "", False
                tail = joined[-(len(stop_stream_at) - 1) :]
            await sink.feed(chunk.kind, text)
    finally:
        if ticker is not None:
            ticker.cancel()
        if sink is not None:
            await sink.close()
    assert done is not None, "the sandbox always ends a command with an exit chunk"
    return ExecOutcome(
        "".join(out),
        "".join(err),
        done.exit_code,
        done.timed_out,
        done.truncated,
        done.limit_exceeded,
        done.duration_ms,
    )


async def _tick(sink: OutputCoalescer) -> None:
    while True:
        await asyncio.sleep(TICK_SECONDS)
        await sink.flush_due()


def _seconds(ms: int) -> str:
    return f"{ms / 1000:.1f} s"


def _status_line(plan: ExecPlan, o: ExecOutcome) -> str:
    if o.timed_out:
        return f"timed out after {plan.timeout_s} s (the command was stopped and has no exit code)"
    if o.limit_exceeded == "disk":
        return "stopped: the workspace grew past its disk limit"
    return f"exit code {o.exit_code} in {_seconds(o.duration_ms)}"


async def _workspace_note(ctx: ToolContext) -> tuple[str, dict[str, Any]]:
    """Record what the command changed in the workspace, and say so."""
    if ctx.workspace is None:
        return "", {}
    changed = await ctx.workspace.sync()
    if changed is None:
        return "", {}
    shown = ", ".join(changed.changed_paths[:8]) + (" …" if len(changed.changed_paths) > 8 else "")
    return (
        f"workspace changed by the command: {len(changed.changed_paths)} files (revision {changed.revision}): {shown}",
        {"workspace_revision": changed.revision, "files_changed": len(changed.changed_paths)},
    )


class ExecTool(Tool):
    tool_class = "exec"

    async def prepare(self, ctx: ToolContext, args: Any) -> ExecPlan | AgentToolResultV1:
        raise NotImplementedError

    async def run(self, ctx: ToolContext, args: Any) -> AgentToolResultV1:
        plan = await self.prepare(ctx, args)
        return plan if isinstance(plan, AgentToolResultV1) else await self.run_plan(ctx, plan)

    async def run_plan(self, ctx: ToolContext, plan: ExecPlan) -> AgentToolResultV1:
        raise NotImplementedError


# --- run_command ----------------------------------------------------------------------------------


class RunCommandArgs(AgentToolArgs):
    argv: (
        Annotated[list[Annotated[str, Field(max_length=4096)]], Field(min_length=1, max_length=64)]
        | None
    ) = None
    shell: Annotated[str, Field(min_length=1, max_length=4096)] | None = None
    cwd: Annotated[str, Field(max_length=1024)] = "."
    timeout_s: Annotated[int, Field(ge=1, le=600)] = 120
    env: dict[str, Annotated[str, Field(max_length=4096)]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_form(self) -> "RunCommandArgs":
        if (self.argv is None) == (self.shell is None):
            raise ValueError("give either argv or shell, not both and not neither")
        return self


class RunCommand(ExecTool):
    name = "run_command"
    description = (
        "Run a command in the workspace sandbox (no network). Give `argv`, a list such as "
        '["npm", "run", "build"]; no shell is involved, so quoting is not needed. Use `shell` (a command '
        "string run by sh -c) only for pipes or redirects; it always needs approval. The result starts with "
        "the exact exit code. Files the command changes become a new workspace revision. Processes do not "
        "outlive the command."
    )
    args_model = RunCommandArgs

    async def prepare(self, ctx: ToolContext, args: RunCommandArgs) -> ExecPlan | AgentToolResultV1:
        try:
            cwd = normalize_workspace_path(args.cwd, allow_root=True)
        except PathRejected:
            return fail(
                "denied", "path_rejected", "That working directory is not allowed in the workspace."
            )
        argv = tuple(args.argv) if args.argv is not None else ("sh", "-c", args.shell or "")
        if not argv[0]:
            return fail("invalid", "invalid_arguments", "The program name is empty.")
        if (why := refuse(args.argv or (), args.shell)) is not None:
            return fail("denied", "command_refused", f"That command is not run: {why}.")
        try:
            ExecRequestV1(argv=argv, cwd=cwd, timeout_s=args.timeout_s, env=args.env)
        except ValidationError:
            return fail(
                "invalid",
                "invalid_arguments",
                "The environment or arguments are not allowed (for example LD_PRELOAD).",
            )
        shell = args.shell is not None
        return ExecPlan(
            argv=argv,
            cwd=cwd,
            timeout_s=args.timeout_s,
            env=dict(args.env),
            shell=shell,
            risk=classify(args.argv or (), args.shell),
            display=_display(argv, cwd, args.shell),
            rule_key=shlex.join(args.argv) if args.argv else f"sh -c {args.shell}",
        )

    async def run_plan(self, ctx: ToolContext, plan: ExecPlan) -> AgentToolResultV1:
        try:
            outcome = await execute(ctx, plan)
        except ExecRejected as rejected:
            return fail("invalid", "exec_rejected", f"The sandbox refused the command: {rejected}.")
        note, extra = await _workspace_note(ctx)
        return _command_result(plan, outcome, note, extra)


def _command_result(
    plan: ExecPlan, o: ExecOutcome, note: str, extra: dict[str, Any]
) -> AgentToolResultV1:
    parts = [f"$ {plan.display}", _status_line(plan, o)]
    if o.truncated:
        parts.append("output was cut at the sandbox limit (4 MiB); the middle is missing")
    if note:
        parts.append(note)
    if o.stdout and o.stderr:
        parts += ["--- stdout ---", o.stdout.rstrip("\n"), "--- stderr ---", o.stderr.rstrip("\n")]
    elif o.stdout or o.stderr:
        parts.append((o.stdout or o.stderr).rstrip("\n"))
    else:
        parts.append("(no output)")
    result = succeed(
        "\n".join(parts),
        summary=_status_line(plan, o)[:200],
        partial=o.truncated or o.limit_exceeded is not None,
        data={
            "exit_code": o.exit_code,
            "timed_out": o.timed_out,
            "duration_ms": o.duration_ms,
            "truncated": o.truncated,
            "limit_exceeded": o.limit_exceeded,
            "output_bytes": len(o.stdout.encode()) + len(o.stderr.encode()),
            "risk": plan.risk,
            **extra,
        },
    )
    sizes = len(o.stdout.encode()) + len(o.stderr.encode())
    result = result.model_copy(update={"bytes_total": max(result.bytes_total, sizes)})
    if o.timed_out:
        result = result.model_copy(update={"status": "timed_out", "error_code": "timed_out"})
    return result


# --- run_tests ------------------------------------------------------------------------------------


class RunTestsArgs(AgentToolArgs):
    runner: Annotated[str, Field(pattern=r"^(auto|" + "|".join(RUNNERS) + r")$")] = "auto"
    selector: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    keyword: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    cwd: Annotated[str, Field(max_length=1024)] = "."
    timeout_s: Annotated[int, Field(ge=1, le=600)] = 600

    @model_validator(mode="after")
    def _no_flags(self) -> "RunTestsArgs":
        for value in (self.selector, self.keyword):
            if value is not None and (value.startswith("-") or "\x00" in value):
                raise ValueError("a selector or keyword cannot start with '-' or contain NUL")
        return self


class RunTests(ExecTool):
    name = "run_tests"
    description = (
        "Run the project's tests and get a structured result: passed, failed and skipped counts and each "
        "failure's name, file, line and message. The runner (pytest, vitest, jest, go or cargo) is detected "
        "unless you name it. `selector` limits to a path or test id; `keyword` filters by name."
    )
    args_model = RunTestsArgs

    async def prepare(self, ctx: ToolContext, args: RunTestsArgs) -> ExecPlan | AgentToolResultV1:
        try:
            cwd = normalize_workspace_path(args.cwd, allow_root=True)
        except PathRejected:
            return fail(
                "denied", "path_rejected", "That working directory is not allowed in the workspace."
            )
        runner: Runner
        if args.runner == "auto":
            prefix = "" if cwd == "." else cwd + "/"
            paths = [
                p[len(prefix) :]
                for p in await ctx.sandbox.file_hashes(ctx.handle)
                if p.startswith(prefix)
            ]
            package = None
            if "package.json" in paths:
                try:
                    package = (
                        await ctx.sandbox.read_bytes(ctx.handle, prefix + "package.json")
                    ).decode("utf-8", "replace")
                except (
                    Exception
                ):  # an unreadable package.json only means no JavaScript runner is detected
                    package = None
            found = detect_runners(paths, package)
            if not found:
                return fail(
                    "invalid",
                    "no_runner",
                    "No test runner was detected (looked for pytest, vitest, jest, go and cargo). Name one with `runner`, or use run_command.",
                )
            runner = found[0]
        else:
            runner = args.runner  # type: ignore[assignment]
        command = command_for(runner, args.selector, args.keyword)
        return ExecPlan(
            argv=command.argv,
            cwd=cwd,
            timeout_s=args.timeout_s,
            env={},
            shell=False,
            risk="medium",
            display=_display(
                command.argv
                if not command.reads_report
                else (
                    runner,
                    *(["-k", args.keyword] if args.keyword else []),
                    *([args.selector] if args.selector else []),
                ),
                cwd,
                None,
            ),
            rule_key=f"run_tests {runner}",
            runner=runner,
        )

    async def run_plan(self, ctx: ToolContext, plan: ExecPlan) -> AgentToolResultV1:
        assert plan.runner is not None
        reads_report = plan.argv[0] == "sh"
        try:
            outcome = await execute(
                ctx, plan, stop_stream_at=REPORT_MARKER if reads_report else None
            )
        except ExecRejected as rejected:
            return fail("invalid", "exec_rejected", f"The sandbox refused the command: {rejected}.")
        note, extra = await _workspace_note(ctx)
        output, report = split_report(outcome.stdout) if reads_report else (outcome.stdout, None)
        summary = None if outcome.timed_out else summarize(plan.runner, output, report)
        return _test_result(plan, outcome, output, summary, note, extra)


def _test_result(
    plan: ExecPlan, o: ExecOutcome, output: str, summary: Any, note: str, extra: dict[str, Any]
) -> AgentToolResultV1:
    runner = plan.runner or "tests"
    leaked = 0
    if summary is not None:
        summary, leaked = redact_summary(summary)
    parts = [f"$ {plan.display}", _status_line(plan, o)]
    if summary is not None:
        parts.insert(
            1,
            f"{runner}: {summary.passed} passed, {summary.failed} failed, {summary.skipped} skipped",
        )
        if o.exit_code not in (0, None) and summary.failed == 0:
            parts.append(
                f"exit code {o.exit_code} with no failed test: the run may not have started; read the output"
            )
        if summary.failures:
            parts.append("Failures:")
            for f in summary.failures[:20]:
                where = f"{f.file}:{f.line}" if f.file and f.line else (f.file or "")
                parts.append(
                    f"  {f.name}"
                    + (f"  ({where})" if where else "")
                    + (f"\n    {f.message}" if f.message else "")
                )
            if len(summary.failures) > 20:
                parts.append(f"  … and {len(summary.failures) - 20} more")
    elif not o.timed_out:
        parts.append("The runner's report could not be read; the raw output follows.")
    if o.truncated:
        parts.append("output was cut at the sandbox limit (4 MiB)")
    if note:
        parts.append(note)
    body = output.rstrip("\n")
    if o.stderr.strip():
        body += ("\n" if body else "") + "--- stderr ---\n" + o.stderr.rstrip("\n")
    if body:
        from app.services.agent.tools.text import truncate_head_tail

        parts += ["--- output ---", truncate_head_tail(body, MAX_SECTION_BYTES).text]
    result = succeed(
        "\n".join(parts),
        summary=(parts[1] if summary is not None else _status_line(plan, o))[:200],
        partial=o.truncated or o.limit_exceeded is not None,
        data={
            "exit_code": o.exit_code,
            "timed_out": o.timed_out,
            "duration_ms": o.duration_ms,
            "runner": runner,
            "parsed": summary is not None,
            **extra,
        },
    )
    update: dict[str, Any] = {"tests": summary, "redactions": result.redactions + leaked}
    if o.timed_out:
        update |= {"status": "timed_out", "error_code": "timed_out"}
    return result.model_copy(update=update)


def exec_plan_to_json(plan: ExecPlan) -> dict[str, object]:
    return {
        "argv": list(plan.argv),
        "cwd": plan.cwd,
        "timeout_s": plan.timeout_s,
        "env": plan.env,
        "shell": plan.shell,
        "risk": plan.risk,
        "display": plan.display,
        "rule_key": plan.rule_key,
        "runner": plan.runner,
    }


def exec_plan_from_json(data: dict[str, Any]) -> ExecPlan:
    return ExecPlan(
        argv=tuple(data["argv"]),
        cwd=data["cwd"],
        timeout_s=data["timeout_s"],
        env=dict(data["env"]),
        shell=data["shell"],
        risk=data["risk"],
        display=data["display"],
        rule_key=data["rule_key"],
        runner=data["runner"],
    )
