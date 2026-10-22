"""Fixed offline argv for owned aliases; uploaded paths/configuration are forbidden."""

import re

from app.scanners.runtime import ToolRuntime


def semgrep_command(aliases: tuple[str, ...], *, runtime: ToolRuntime) -> tuple[str, ...]:
    if (
        runtime.tool != "semgrep"
        or not 1 <= len(aliases) <= 128
        or len(set(aliases)) != len(aliases)
        or any(not re.fullmatch(r"[0-9a-f]{32}\.py", alias) for alias in aliases)
    ):
        raise ValueError("invalid scanner selection")
    return (
        str(runtime.executable),
        "scan",
        "--config",
        str(runtime.rules),
        "--oss-only",
        "--json",
        "--metrics",
        "off",
        "--disable-version-check",
        "--disable-nosem",
        "--no-git-ignore",
        "--no-rewrite-rule-ids",
        "--strict",
        "--no-error",
        "--jobs",
        "1",
        "--timeout",
        "20",
        "--timeout-threshold",
        "1",
        "--max-memory",
        "768",
        "--max-target-bytes",
        "2097152",
        *aliases,
    )


def gitleaks_command(alias: str, *, runtime: ToolRuntime) -> tuple[str, ...]:
    if (
        runtime.tool != "gitleaks"
        or runtime.ignore_file is None
        or not re.fullmatch(r"[0-9a-f]{32}\.txt", alias)
    ):
        raise ValueError("invalid scanner selection")
    return (
        str(runtime.executable),
        "dir",
        "--config",
        str(runtime.rules),
        "--enable-rule",
        "secret.github-classic-pat",
        "--ignore-gitleaks-allow",
        "--gitleaks-ignore-path",
        str(runtime.ignore_file),
        "--redact=100",
        "--max-decode-depth",
        "0",
        "--max-archive-depth",
        "0",
        "--max-target-megabytes",
        "3",
        "--timeout",
        "20",
        "--log-level",
        "error",
        "--exit-code",
        "10",
        "--no-banner",
        "--no-color",
        "--report-format",
        "json",
        "--report-path",
        "-",
        alias,
    )
