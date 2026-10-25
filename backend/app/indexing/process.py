"""Separate 24 MiB index pipe profile over the shared cancellation-safe process core."""

from pathlib import Path

from app.scanners.process import NativeResult, _run_bounded


async def run_index_private(
    argv: tuple[str, ...],
    *,
    deadline: float,
    stdout_limit: int,
    stderr_limit: int,
    cwd: Path | None = None,
) -> NativeResult:
    return await _run_bounded(
        argv,
        deadline=deadline,
        stdout_limit=stdout_limit,
        stderr_limit=stderr_limit,
        cwd=cwd,
        output_ceiling=25_165_824,
    )
