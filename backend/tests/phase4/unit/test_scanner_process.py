"""Exercise real owned children with synthetic output, never uploaded code."""

import asyncio
import os
import sys

import pytest
from app.scanners.process import NativeProcessError, run_private


async def execute(code, **kwargs):
    return await run_private((sys.executable, "-c", code), **kwargs)


async def test_drains_both_pipes_concurrently_without_deadlock():
    result = await execute(
        "import os; os.write(2,b'e'*60000); os.write(1,b'o'*60000)",
        deadline=2,
        stdout_limit=65536,
        stderr_limit=65536,
    )
    assert result.output == b"o" * 60000
    assert result.exit_code == 0
    assert not hasattr(result, "stderr")


@pytest.mark.parametrize("pipe", [1, 2])
async def test_output_overflow_is_fixed_error_and_child_is_reaped(pipe):
    with pytest.raises(NativeProcessError, match=r"^scanner_output_limit$"):
        await execute(
            f"import os; os.write({pipe},b'CANARY'*100000)",
            deadline=2,
            stdout_limit=100,
            stderr_limit=100,
        )


async def test_deadline_kills_and_reaps_hanging_child():
    with pytest.raises(NativeProcessError, match=r"^scanner_timeout$"):
        await execute(
            "import time; time.sleep(60)", deadline=0.05, stdout_limit=100, stderr_limit=100
        )


async def test_cancellation_reaps_owned_process_group(tmp_path):
    pidfile = tmp_path / "pid"
    code = (
        "import os,time; from pathlib import Path; "
        f"Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    task = asyncio.create_task(execute(code, deadline=2, stdout_limit=100, stderr_limit=100))
    async with asyncio.timeout(2):
        while not pidfile.exists():  # noqa: ASYNC110 - external child readiness, no in-process event
            await asyncio.sleep(0.005)
    pid = int(pidfile.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_process_does_not_inherit_provider_or_other_environment(monkeypatch):
    monkeypatch.setenv("CODESPY_SYNTHETIC_PRIVATE", "CANARY")
    result = await execute(
        "import os; print(os.environ.get('CODESPY_SYNTHETIC_PRIVATE','absent'))",
        deadline=2,
        stdout_limit=100,
        stderr_limit=100,
    )
    assert result.output == b"absent\n"


async def test_native_stderr_and_exit_text_are_not_returned_or_raised():
    result = await execute(
        "import sys; sys.stderr.write('CANARY'); sys.exit(7)",
        deadline=2,
        stdout_limit=100,
        stderr_limit=100,
    )
    assert result.exit_code == 7
    assert result.output == b""
    assert "CANARY" not in repr(result)


async def test_deadline_includes_waiting_for_process_creation(monkeypatch):
    ready = asyncio.Event()

    async def delayed_start(*args, **kwargs):
        await ready.wait()
        raise AssertionError("must be cancelled before process creation")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_start)
    with pytest.raises(NativeProcessError, match=r"^scanner_timeout$"):
        async with asyncio.timeout(1):
            await execute("pass", deadline=0.01, stdout_limit=100, stderr_limit=100)


async def test_diagnostics_presence_is_retained_without_retaining_native_text():
    result = await execute(
        "import sys; sys.stderr.write('CANARY')", deadline=2, stdout_limit=100, stderr_limit=100
    )
    assert result.diagnostic_bytes == 6
    assert "CANARY" not in repr(result)


async def test_child_home_is_private_workdir_not_inherited_user_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/unrelated-user-home")
    result = await execute(
        "import os; print(os.environ.get('HOME','absent'))",
        cwd=tmp_path,
        deadline=2,
        stdout_limit=1024,
        stderr_limit=100,
    )
    assert result.output.decode().strip() == str(tmp_path)


async def test_cancelled_child_keeps_cancellation_when_reaper_reports_failure(
    tmp_path, monkeypatch
):
    from app.scanners import process as primitive

    pidfile = tmp_path / "cancel-reaper-pid"
    original = primitive._reap

    async def uncertain(process, tasks):
        await original(process, tasks)
        raise NativeProcessError("scanner_cleanup_pending")

    monkeypatch.setattr(primitive, "_reap", uncertain)
    code = (
        "import os,time; from pathlib import Path; "
        + f"Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    task = asyncio.create_task(execute(code, deadline=2, stdout_limit=100, stderr_limit=100))
    async with asyncio.timeout(2):
        while not pidfile.exists():  # noqa: ASYNC110 - external child readiness
            await asyncio.sleep(0.005)
    pid = int(pidfile.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
