"""Private native pipes for trusted scanner argv inside an isolated worker.

This primitive is not a sandbox. Callers must establish OS/container isolation;
never pass uploaded commands, executable paths, environment or configuration.
"""

import asyncio
import os
import signal
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from app.domain.errors import PersistenceError


class NativeProcessError(PersistenceError):
    pass


@dataclass(frozen=True, slots=True)
class NativeResult:
    exit_code: int
    output: bytes = field(repr=False)
    diagnostic_bytes: int = 0


async def _read(stream: asyncio.StreamReader, maximum: int, *, retain: bool) -> tuple[bytes, int]:
    output = bytearray()
    size = 0
    while block := await stream.read(65536):
        size += len(block)
        if size > maximum:
            raise NativeProcessError("scanner_output_limit")
        if retain:
            output.extend(block)
    return bytes(output), size


async def _reap(process: asyncio.subprocess.Process, tasks: list[asyncio.Task[object]]) -> None:
    # The child starts a new session; only its owned process group is signalled.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    async def discard(stream: asyncio.StreamReader | None) -> None:
        if stream is not None:
            while await stream.read(65536):
                pass

    async with asyncio.timeout(5):
        await asyncio.gather(discard(process.stdout), discard(process.stderr), process.wait())


async def run_private(
    argv: tuple[str, ...],
    *,
    deadline: float,
    stdout_limit: int,
    stderr_limit: int,
    cwd: Path | None = None,
) -> NativeResult:
    """Discard diagnostics, bound both pipes, and reap on every exit path."""
    if not argv or not 0 < deadline <= 180 or not 0 < stdout_limit <= 2097152:
        raise ValueError("invalid scanner process limits")
    if not 0 < stderr_limit <= 65536:
        raise ValueError("invalid scanner process limits")
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "SEMGREP_SEND_METRICS": "off",
        "SEMGREP_ENABLE_VERSION_CHECK": "0",
    }
    if cwd is not None:
        environment["HOME"] = str(cwd)
        environment["XDG_CACHE_HOME"] = str(cwd / ".cache")
    process: asyncio.subprocess.Process | None = None
    tasks: list[asyncio.Task[object]] = []
    try:
        async with asyncio.timeout(deadline):
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                cwd=cwd,
                env=environment,
                limit=65536,
            )
            assert process.stdout is not None and process.stderr is not None
            output = asyncio.create_task(_read(process.stdout, stdout_limit, retain=True))
            diagnostic = asyncio.create_task(_read(process.stderr, stderr_limit, retain=False))
            waiter = asyncio.create_task(process.wait())
            tasks = [output, diagnostic, waiter]
            await asyncio.gather(*tasks)
            return NativeResult(
                exit_code=waiter.result(),
                output=output.result()[0],
                diagnostic_bytes=diagnostic.result()[1],
            )
    except TimeoutError:
        raise NativeProcessError("scanner_timeout") from None
    finally:
        if process is not None:
            cleanup = asyncio.create_task(_reap(process, tasks))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                with suppress(Exception):
                    await cleanup
                raise
            except Exception:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise asyncio.CancelledError() from None
                raise NativeProcessError("scanner_cleanup_pending") from None
