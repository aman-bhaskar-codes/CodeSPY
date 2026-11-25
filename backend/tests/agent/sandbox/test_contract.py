"""The sandbox contract against the in-memory fake. Real sandboxes subclass the same suite."""

from app.adapters.sandbox.fake import FakeSandbox, MappingSnapshots, ScriptedExec

from tests.agent.sandbox.contract import SandboxContract


class TestFakeSandboxContract(SandboxContract):
    async def build(self, snapshots):
        return FakeSandbox(MappingSnapshots(snapshots))

    def cmd(self, sandbox, *, stdout="", stderr="", exit_code=0, delay_s=0.0, stdout_bytes=0):
        argv = ("scripted", str(len(sandbox.exec_calls)), str(id(object())))
        sandbox.script(
            argv[:2],
            ScriptedExec(
                stdout="x" * stdout_bytes if stdout_bytes else stdout,
                stderr=stderr,
                exit_code=exit_code,
                delay_s=delay_s,
            ),
        )
        return argv
