# ruff: noqa: E501
"""Runner detection and the parsers for what each runner reports."""

from pathlib import Path

import pytest
from app.services.agent.tools.runners import (
    REPORT_MARKER,
    command_for,
    detect_runners,
    parse_cargo,
    parse_go_json,
    parse_jest_json,
    parse_junit,
    split_report,
    summarize,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_pytest_junit_is_read_exactly():
    s = parse_junit(fixture("pytest-junit.xml"), "pytest")
    assert (s.runner, s.passed, s.failed, s.skipped) == ("pytest", 2, 2, 1)
    assert [f.name for f in s.failures] == [
        "tests.test_auth.TestToken::test_expiry",
        "tests.test_auth::test_refresh",
    ]
    first, second = s.failures
    assert (
        first.file == "tests/test_auth.py" and first.message == "assert 1 == 2"
    )  # class segments are dropped from the path
    assert (second.file, second.line) == (
        "tests/test_auth.py",
        71,
    ) and "fixture 'client' not found" in second.message
    assert s.duration_ms == 412  # the suite's own time, which includes setup


def test_vitest_junit_is_read_exactly():
    s = parse_junit(fixture("vitest-junit.xml"), "vitest")
    assert (s.passed, s.failed, s.skipped) == (2, 1, 1) and s.duration_ms == 350
    [failure] = s.failures
    assert failure.name == "multiplies > adds" and failure.file == "src/math.test.ts"
    assert failure.message.startswith("expected 6 to be 7")


def test_jest_json_is_read_exactly():
    s = parse_jest_json(fixture("jest-report.json"))
    assert (s.passed, s.failed, s.skipped) == (2, 1, 1)
    [failure] = s.failures
    assert (
        failure.name == "a subtracts"
        and failure.file.endswith("a.test.js")
        and "Expected: 1 Received: 2" in failure.message
    )


def test_go_json_is_read_exactly():
    s = parse_go_json(fixture("go-test.jsonl"))
    assert (s.passed, s.failed, s.skipped) == (1, 1, 1)
    [failure] = s.failures
    assert failure.name == "example.com/m/pkg.TestSub" and "got 3, want 4" in failure.message


def test_cargo_text_is_read_exactly():
    s = parse_cargo(fixture("cargo-test.txt"))
    assert (s.passed, s.failed, s.skipped) == (1, 1, 1) and [f.name for f in s.failures] == [
        "tests::subs"
    ]


def test_a_report_that_cannot_be_read_is_none_not_a_guess():
    for bad in ("", "not xml", "<testsuites></testsuites>", "<a><b></a>"):
        assert parse_junit(bad, "pytest") is None
    assert parse_jest_json("{}") is None and parse_jest_json("nope") is None
    assert parse_go_json("plain output\n") is None and parse_cargo("nothing here") is None


def test_hostile_xml_is_refused_before_parsing():
    bomb = '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]><testsuite><testcase name="&b;"/></testsuite>'
    assert parse_junit(bomb, "pytest") is None
    external = '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><testsuite><testcase name="&e;"/></testsuite>'
    assert parse_junit(external, "pytest") is None
    assert (
        parse_junit(
            "<testsuite>" + "<testcase name='x'/>" * 50 + "</testsuite>" + " " * (3 * 1024 * 1024),
            "pytest",
        )
        is None
    )  # oversized


def test_at_most_fifty_failures_are_kept_but_all_are_counted():
    cases = "".join(
        f'<testcase classname="t" name="n{i}"><failure message="bad {i}"/></testcase>'
        for i in range(80)
    )
    s = parse_junit(f"<testsuite>{cases}</testsuite>", "vitest")
    assert s.failed == 80 and len(s.failures) == 50


def test_long_failure_messages_are_clipped():
    s = parse_junit(
        '<testsuite><testcase classname="t" name="n"><failure message="'
        + "x" * 3000
        + '"/></testcase></testsuite>',
        "vitest",
    )
    assert len(s.failures[0].message) <= 500


def test_split_report_separates_output_from_the_report():
    out, report = split_report(f"1 passed\n\n{REPORT_MARKER}\n<xml/>\n")
    assert out == "1 passed" and report == "<xml/>"
    assert split_report("no marker here") == ("no marker here", None)
    assert split_report(f"out\n{REPORT_MARKER}\n") == ("out", None)  # the runner wrote no report


def test_summarize_routes_by_runner():
    assert summarize("pytest", "", fixture("pytest-junit.xml")).failed == 2
    assert summarize("pytest", "", None) is None
    assert summarize("go", fixture("go-test.jsonl"), None).runner == "go"
    assert summarize("cargo", fixture("cargo-test.txt"), None).runner == "cargo"


@pytest.mark.parametrize(
    ("paths", "package", "expected"),
    [
        (["pyproject.toml", "src/a.py"], None, ["pytest"]),
        (["tests/test_a.py"], None, ["pytest"]),
        (["a_test.py"], None, ["pytest"]),
        (["package.json"], '{"devDependencies": {"vitest": "^1"}}', ["vitest"]),
        (["package.json"], '{"devDependencies": {"jest": "^29"}}', ["jest"]),
        (["package.json"], '{"scripts": {"test": "vitest run"}}', ["vitest"]),
        (["package.json"], '{"devDependencies": {"vitest": "1", "jest": "2"}}', ["vitest"]),
        (["package.json"], "not json", []),
        (["go.mod", "main.go"], None, ["go"]),
        (["Cargo.toml"], None, ["cargo"]),
        (
            ["pyproject.toml", "package.json", "go.mod"],
            '{"devDependencies": {"jest": "1"}}',
            ["pytest", "jest", "go"],
        ),
        (["README.md"], None, []),
    ],
)
def test_runner_detection(paths, package, expected):
    assert detect_runners(paths, package) == expected


def test_commands_never_put_a_selector_where_it_could_become_a_flag():
    pytest_cmd = command_for("pytest", "tests/test_a.py::test_x", "login")
    assert pytest_cmd.argv[:2] == ("sh", "-c") and pytest_cmd.argv[3:] == (
        "runner",
        "-k",
        "login",
        "tests/test_a.py::test_x",
    )
    assert (
        "python3 -m pytest -q -p no:cacheprovider --junitxml=" in pytest_cmd.argv[2]
        and '"$@"' in pytest_cmd.argv[2]
    )
    assert command_for("go", None, None).argv == ("go", "test", "-json", "./...")
    assert command_for("go", "./pkg/...", "TestX").argv == (
        "go",
        "test",
        "-json",
        "./pkg/...",
        "-run",
        "TestX",
    )
    assert command_for("cargo", "tests::subs", None).argv == ("cargo", "test", "--", "tests::subs")
    assert (
        command_for("vitest", None, None).reads_report
        and not command_for("go", None, None).reads_report
    )
