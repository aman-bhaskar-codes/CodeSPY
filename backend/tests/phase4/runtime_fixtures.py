"""Trusted synthetic layouts for scanner command and boundary tests."""

from pathlib import Path
from typing import Literal

from app.scanners.runtime import ToolRuntime


def runtime(tool: Literal["semgrep", "gitleaks"]) -> ToolRuntime:
    return ToolRuntime(
        tool=tool,
        version="1.179.0" if tool == "semgrep" else "8.30.1",
        executable=Path("/usr/local/bin") / tool,
        artifact=Path("/trusted/core"),
        rules=Path("/opt/codespy/rules") / ("source.yml" if tool == "semgrep" else "secrets.toml"),
        ignore_file=Path("/opt/codespy/rules/empty.ignore") if tool == "gitleaks" else None,
        artifact_sha256="b" * 64,
        rules_sha256="c" * 64,
        rule_ids=("python.shell-true",) if tool == "semgrep" else ("secret.github-classic-pat",),
    )
