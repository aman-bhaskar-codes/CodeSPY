"""The sandbox contract against real containers. Opt in with CODESPY_SANDBOX_IMAGE."""

import pytest

from tests.agent.sandbox.contract import SandboxContract
from tests.agent.sandbox.docker_support import IMAGE, Harness, requires_docker

pytestmark = [pytest.mark.sandbox, requires_docker]


class TestDockerSandboxContract(SandboxContract):
    image_name = IMAGE

    @pytest.fixture(autouse=True)
    async def _cleanup(self):
        self.harnesses = []
        yield
        for harness in self.harnesses:
            await harness.close()

    async def build(self, snapshots):
        harness = Harness(snapshots)
        self.harnesses.append(harness)
        return harness.sandbox

    def cmd(self, sandbox, *, stdout="", stderr="", exit_code=0, delay_s=0.0, stdout_bytes=0):
        steps = []
        if stdout_bytes:
            steps.append(f"head -c {stdout_bytes} /dev/zero | tr '\\0' x")
        if stdout:
            steps.append('printf %s "$0"')
        if stderr:
            steps.append('printf %s "$1" >&2')
        if delay_s:
            steps.append(f"sleep {delay_s}")
        steps.append(f"exit {exit_code}")
        return ("sh", "-c", ";".join(steps), stdout, stderr)
