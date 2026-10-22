"""Only generated aliases and reviewed offline tool options reach native argv."""

from uuid import UUID

import pytest
from app.scanners.commands import gitleaks_command, semgrep_command
from tests.phase4.runtime_fixtures import runtime

ALIAS = UUID(int=1).hex + ".py"


def test_semgrep_uses_only_authored_rules_and_disables_target_suppression():
    args = semgrep_command((ALIAS,), runtime=runtime("semgrep"))
    assert args[:2] == ("/usr/local/bin/semgrep", "scan")
    for flag in (
        "--oss-only",
        "--disable-nosem",
        "--disable-version-check",
        "--no-git-ignore",
        "--no-rewrite-rule-ids",
        "--strict",
    ):
        assert flag in args
    assert args[args.index("--config") + 1] == "/opt/codespy/rules/source.yml"
    assert "auto" not in args
    assert args[-1] == ALIAS


def test_gitleaks_disables_decoding_archives_and_target_suppression():
    args = gitleaks_command(UUID(int=1).hex + ".txt", runtime=runtime("gitleaks"))
    assert args[:2] == ("/usr/local/bin/gitleaks", "dir")
    assert "--ignore-gitleaks-allow" in args
    for flag in ("--max-decode-depth", "--max-archive-depth"):
        assert args[args.index(flag) + 1] == "0"
    assert "--redact=100" in args
    assert args[args.index("--config") + 1] == "/opt/codespy/rules/secrets.toml"
    assert "--diagnostics" not in args


@pytest.mark.parametrize(
    "alias",
    [
        "../escape.py",
        "--config",
        "/etc/passwd",
        "canary.py",
        UUID(int=1).hex + ".py/evil",
        UUID(int=1).hex + ".txt",
    ],
)
def test_semgrep_cannot_receive_target_paths_or_nonpython_alias(alias):
    with pytest.raises(ValueError):
        semgrep_command((alias,), runtime=runtime("semgrep"))


def test_scanner_invocations_require_nonempty_bounded_unique_selection():
    for files in ((), (ALIAS, ALIAS), tuple(UUID(int=n).hex + ".py" for n in range(129))):
        with pytest.raises(ValueError):
            semgrep_command(files, runtime=runtime("semgrep"))
    with pytest.raises(ValueError):
        gitleaks_command("../../escape", runtime=runtime("gitleaks"))


def test_gitleaks_findings_have_a_distinct_nonerror_exit_status():
    args = gitleaks_command(UUID(int=1).hex + ".txt", runtime=runtime("gitleaks"))
    assert args[args.index("--exit-code") + 1] == "10"
    assert args[args.index("--log-level") + 1] == "error"
