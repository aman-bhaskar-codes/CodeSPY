"""Trusted runtime wiring controls executables; requests cannot supply paths."""

from pathlib import Path
from uuid import UUID

import pytest
from app.scanners.commands import gitleaks_command, semgrep_command
from app.scanners.runtime import ToolRuntime

ALIAS = UUID(int=1).hex


def test_tool_adapter_uses_injected_layout_without_accepting_uploaded_configuration():
    runtime = ToolRuntime(
        tool="semgrep",
        version="1.179.0",
        executable=Path("/trusted/bin/scan"),
        artifact=Path("/trusted/bin/core"),
        rules=Path("/trusted/rules/authored.yml"),
        rules_sha256="a" * 64,
        artifact_sha256="b" * 64,
        rule_ids=("python.shell-true",),
    )
    command = semgrep_command((ALIAS + ".py",), runtime=runtime)
    assert command[0] == "/trusted/bin/scan"
    assert command[command.index("--config") + 1] == "/trusted/rules/authored.yml"
    assert "--metrics" in command and "--disable-nosem" in command
    with pytest.raises(ValueError):
        gitleaks_command(ALIAS + ".txt", runtime=runtime)


@pytest.mark.parametrize("path", ["relative/tool", "/trusted/../etc/tool", "https://host/tool"])
def test_runtime_rejects_ambiguous_or_remote_executable_location(path):
    with pytest.raises(ValueError):
        ToolRuntime(
            tool="semgrep",
            version="1.179.0",
            executable=Path(path),
            artifact=Path("/trusted/core"),
            rules=Path("/trusted/rules.yml"),
            rules_sha256="a" * 64,
            artifact_sha256="b" * 64,
            rule_ids=("known",),
        )
