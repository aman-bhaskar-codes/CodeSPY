"""Index-specific pipe ceiling; container controls remain the shared offline profile."""

from app.indexing.process import run_index_private
from app.scanners.docker import DockerController
from app.scanners.process import NativeProcessError, NativeResult


class IndexDockerController(DockerController):
    async def _run(
        self,
        args: tuple[str, ...],
        *,
        seconds: float = 15,
        maximum: int = 65536,
        accepted_exits: tuple[int, ...] = (0,),
    ) -> NativeResult:
        try:
            result = await run_index_private(
                (
                    str(self.executable),
                    "--host",
                    "unix://" + str(self.socket),
                    "--config",
                    str(self.config_root),
                    *args,
                ),
                deadline=seconds,
                stdout_limit=maximum,
                stderr_limit=65_536,
            )
        except OSError:
            raise NativeProcessError("index_runtime_unavailable") from None
        if result.exit_code not in accepted_exits:
            raise NativeProcessError("index_runtime_unavailable")
        return result
