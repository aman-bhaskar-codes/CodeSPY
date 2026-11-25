"""Which test runner a workspace uses, how to invoke it, and how to read what it reports.

Reports come from inside a sandbox, so they are untrusted input: XML with a DOCTYPE or entity
declarations is refused outright, sizes are capped, and a report that cannot be read yields no
summary rather than a guess (the raw output is still shown).
"""

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from app.contracts.agent_events import TestFailure
from app.contracts.agent_tools import TestSummaryV1

Runner = Literal["pytest", "vitest", "jest", "go", "cargo"]
RUNNERS: tuple[Runner, ...] = ("pytest", "vitest", "jest", "go", "cargo")
REPORT_MARKER = "<<CODESPY-REPORT>>"
REPORT_PATH = "/tmp/codespy-report"  # noqa: S108 -- inside the command's private tmpfs
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_FAILURES = 50
MESSAGE_CHARS = 500


@dataclass(frozen=True)
class RunnerCommand:
    """How to run a runner: an argv, and whether its report follows ``REPORT_MARKER`` on stdout."""

    argv: tuple[str, ...]
    reads_report: bool


def detect_runners(paths: Iterable[str], package_json: str | None) -> list[Runner]:
    """Runners the workspace appears to use, in the order they would be chosen."""
    names = set(paths)
    found: list[Runner] = []
    if names & {"pytest.ini", "conftest.py", "tox.ini", "pyproject.toml", "setup.cfg"} or any(
        p.endswith(".py") and ("test_" in p.rsplit("/", 1)[-1] or p.endswith("_test.py"))
        for p in names
    ):
        found.append("pytest")
    if "package.json" in names and package_json is not None:
        try:
            package = json.loads(package_json)
        except ValueError:
            package = {}
        deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
        scripts = " ".join(map(str, package.get("scripts", {}).values()))
        if "vitest" in deps or "vitest" in scripts:
            found.append("vitest")
        elif "jest" in deps or "jest" in scripts:
            found.append("jest")
    if "go.mod" in names:
        found.append("go")
    if "Cargo.toml" in names:
        found.append("cargo")
    return found


def _framed(script: str, args: list[str]) -> tuple[str, ...]:
    """Run ``script`` through sh with ``args`` as positional parameters, then print the report."""
    tail = (
        f'code=$?; printf "\\n%s\\n" "{REPORT_MARKER}"; cat {REPORT_PATH} 2>/dev/null; exit $code'
    )
    return ("sh", "-c", f"{script}; {tail}", "runner", *args)


def command_for(runner: Runner, selector: str | None, keyword: str | None) -> RunnerCommand:
    targets = [selector] if selector else []
    if runner == "pytest":
        extra = ["-k", keyword] if keyword else []
        return RunnerCommand(
            _framed(
                f'python3 -m pytest -q -p no:cacheprovider --junitxml={REPORT_PATH} "$@"',
                [*extra, *targets],
            ),
            True,
        )
    if runner == "vitest":
        extra = ["-t", keyword] if keyword else []
        return RunnerCommand(
            _framed(
                f'npx --no-install vitest run --reporter=junit --outputFile={REPORT_PATH} "$@"',
                [*extra, *targets],
            ),
            True,
        )
    if runner == "jest":
        extra = ["-t", keyword] if keyword else []
        return RunnerCommand(
            _framed(
                f'npx --no-install jest --ci --json --outputFile={REPORT_PATH} "$@"',
                [*extra, *targets],
            ),
            True,
        )
    if runner == "go":
        return RunnerCommand(
            (
                "go",
                "test",
                "-json",
                *(targets or ["./..."]),
                *(["-run", keyword] if keyword else []),
            ),
            False,
        )
    extra = [keyword] if keyword else []
    return RunnerCommand(("cargo", "test", *extra, *(["--", *targets] if targets else [])), False)


def split_report(stdout: str) -> tuple[str, str | None]:
    """The command's own output and the report printed after the marker, if any."""
    head, marker, report = stdout.partition(REPORT_MARKER)
    return (head.rstrip("\n"), report.strip() or None) if marker else (stdout, None)


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MESSAGE_CHARS else text[: MESSAGE_CHARS - 1] + "…"


def _file_from_classname(classname: str) -> str | None:
    parts = [p for p in classname.split(".") if p]
    while parts and parts[-1][:1].isupper():
        parts.pop()
    return "/".join(parts) + ".py" if parts else None


def parse_junit(report: str, runner: str) -> TestSummaryV1 | None:
    if len(report.encode()) > MAX_REPORT_BYTES or "<!DOCTYPE" in report or "<!ENTITY" in report:
        return None
    try:
        root = ET.fromstring(report)  # noqa: S314 -- guarded above
    except ET.ParseError:
        return None
    passed = failed = skipped = 0
    failures: list[TestFailure] = []
    seconds = 0.0
    for case in root.iter("testcase"):
        seconds += float(case.get("time") or 0)
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if case.find("skipped") is not None:
            skipped += 1
        elif problem is not None:
            failed += 1
            if len(failures) < MAX_FAILURES:
                classname, name = case.get("classname") or "", case.get("name") or "test"
                location = case.get("file") or (
                    _file_from_classname(classname) if runner == "pytest" else classname or None
                )
                line = case.get("line")
                detail = (problem.text or "").strip().splitlines()
                text = problem.get("message") or (detail[0] if detail else "")
                failures.append(
                    TestFailure(
                        name=f"{classname}::{name}" if classname and runner == "pytest" else name,
                        file=location or None,
                        line=int(line) if line and line.isdigit() and int(line) > 0 else None,
                        message=_clip(text),
                    )
                )
        else:
            passed += 1
    if passed + failed + skipped == 0:
        return None
    reported = root.get("time")
    if reported is None:
        suites = [float(t.get("time") or 0) for t in root.iter("testsuite")]
        reported = str(sum(suites)) if suites else None
    return TestSummaryV1(
        runner=runner,
        passed=passed,
        failed=failed,
        skipped=skipped,
        failures=tuple(failures),
        duration_ms=int(float(reported if reported is not None else seconds) * 1000),
    )


def parse_jest_json(report: str) -> TestSummaryV1 | None:
    if len(report.encode()) > MAX_REPORT_BYTES:
        return None
    try:
        data = json.loads(report)
        passed, failed = int(data["numPassedTests"]), int(data["numFailedTests"])
        skipped = int(data.get("numPendingTests", 0)) + int(data.get("numTodoTests", 0))
    except (ValueError, KeyError, TypeError):
        return None
    failures: list[TestFailure] = []
    for suite in data.get("testResults", []):
        for case in suite.get("assertionResults", []):
            if case.get("status") == "failed" and len(failures) < MAX_FAILURES:
                messages = case.get("failureMessages") or [""]
                failures.append(
                    TestFailure(
                        name=str(case.get("fullName") or case.get("title") or "test")[:512],
                        file=str(suite.get("name"))[:1024] or None,
                        message=_clip(str(messages[0])),
                    )
                )
    return TestSummaryV1(
        runner="jest", passed=passed, failed=failed, skipped=skipped, failures=tuple(failures)
    )


def parse_go_json(output: str) -> TestSummaryV1 | None:
    passed = failed = skipped = 0
    lines: dict[tuple[str, str], list[str]] = {}
    failing: list[tuple[str, str]] = []
    for raw in output.splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        test, package, action = event.get("Test"), event.get("Package", ""), event.get("Action")
        if not test:
            continue
        key = (package, test)
        if action == "output":
            lines.setdefault(key, []).append(str(event.get("Output", "")))
        elif action == "pass":
            passed += 1
        elif action == "skip":
            skipped += 1
        elif action == "fail":
            failed += 1
            failing.append(key)
    if passed + failed + skipped == 0:
        return None
    failures = [
        TestFailure(name=f"{pkg}.{test}"[:512], message=_clip("".join(lines.get((pkg, test), []))))
        for pkg, test in failing[:MAX_FAILURES]
    ]
    return TestSummaryV1(
        runner="go", passed=passed, failed=failed, skipped=skipped, failures=tuple(failures)
    )


_CARGO = re.compile(r"test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored")
_CARGO_FAIL = re.compile(r"^test (\S+) \.\.\. FAILED$", re.M)


def parse_cargo(output: str) -> TestSummaryV1 | None:
    totals = [tuple(map(int, m.groups())) for m in _CARGO.finditer(output)]
    if not totals:
        return None
    names = _CARGO_FAIL.findall(output)[:MAX_FAILURES]
    return TestSummaryV1(
        runner="cargo",
        passed=sum(t[0] for t in totals),
        failed=sum(t[1] for t in totals),
        skipped=sum(t[2] for t in totals),
        failures=tuple(TestFailure(name=n[:512]) for n in names),
    )


def summarize(runner: Runner, output: str, report: str | None) -> TestSummaryV1 | None:
    """The structured result for what the runner printed, or ``None`` when it cannot be read."""
    if runner in {"pytest", "vitest"}:
        return parse_junit(report, runner) if report else None
    if runner == "jest":
        return parse_jest_json(report) if report else None
    return parse_go_json(output) if runner == "go" else parse_cargo(output)


def runners_by_name() -> Mapping[str, Runner]:
    return {r: r for r in RUNNERS}


def event_payload(tool_call_id: str, summary: TestSummaryV1) -> dict[str, object]:
    """The ``test.result`` event payload for a summary: the runner emits it after the tool call."""
    return {
        "tool_call_id": tool_call_id,
        "runner": summary.runner,
        "passed": summary.passed,
        "failed": summary.failed,
        "skipped": summary.skipped,
        "failures": [f.model_dump(mode="json", exclude_none=True) for f in summary.failures],
    }


def redact_summary(summary: TestSummaryV1) -> tuple[TestSummaryV1, int]:
    """The summary with secrets in failure names, files and messages replaced, and how many."""
    from app.services.agent.tools.text import redact

    count = 0
    failures: list[TestFailure] = []
    for f in summary.failures:
        name, a = redact(f.name)
        file, b = redact(f.file) if f.file else (None, 0)
        message, c = redact(f.message)
        count += a + b + c
        failures.append(TestFailure(name=name, file=file, line=f.line, message=message))
    return summary.model_copy(update={"failures": tuple(failures)}), count
