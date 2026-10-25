"""Real bounded host child processes; no uploaded code or sandbox claims."""

import sys

import pytest
from app.scanners.process import NativeProcessError, run_private


@pytest.mark.asyncio
async def test_index_pipe_allows_bounded_draft_without_expanding_scanner_limit():
    from app.indexing.process import run_index_private

    argv = (sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x'*2097153)")
    with pytest.raises(ValueError):
        await run_private(argv, deadline=5, stdout_limit=2_097_153, stderr_limit=65_536)
    output = await run_index_private(argv, deadline=5, stdout_limit=2_097_153, stderr_limit=65_536)
    assert len(output.output) == 2_097_153


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["stdout", "stderr"])
async def test_index_child_pipe_overflow_is_reaped(channel):
    from app.indexing.process import run_index_private

    argv = (sys.executable, "-c", f"import sys; sys.{channel}.buffer.write(b'x'*65537)")
    with pytest.raises(NativeProcessError):
        await run_index_private(argv, deadline=5, stdout_limit=65_536, stderr_limit=65_536)


@pytest.mark.asyncio
async def test_index_child_does_not_inherit_provider_environment(monkeypatch):
    from app.indexing.process import run_index_private

    monkeypatch.setenv("CODESPY_SYNTHETIC_PROVIDER_SECRET", "canary")
    output = await run_index_private(
        (
            sys.executable,
            "-c",
            "import os; print(os.environ.get('CODESPY_SYNTHETIC_PROVIDER_SECRET','absent'))",
        ),
        deadline=5,
        stdout_limit=1024,
        stderr_limit=65_536,
    )
    assert output.output == b"absent\n"
