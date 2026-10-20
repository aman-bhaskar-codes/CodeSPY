"""Lifecycle fault injection uses owned fake transport, never Docker or target execution."""

import asyncio
import hashlib
import json

import pytest
from app.analyzers.pipeline import analyze
from app.analyzers.runner import AnalysisProcessError, DockerAnalysisRunner
from app.domain.canonical_json import canonical_bytes
from tests.phase3.unit.test_materialization import fixture_input


class Logs:
    def __init__(self):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.returncode = None

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


class OwnedRunner(DockerAnalysisRunner):
    async def readiness(self):
        return "a" * 64

    async def _command(self, *args, **kwargs):
        if args[0] == "inspect":
            return json.dumps(
                {
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,
                    "Privileged": False,
                    "Memory": 1073741824,
                    "MemorySwap": 1073741824,
                    "PidsLimit": 64,
                    "NanoCpus": 2000000000,
                    "CapDrop": ["ALL"],
                }
            ).encode()
        return b""

    async def _remove(self, name):
        self.removed = True


async def test_diagnostic_flood_during_transfer_rejects_valid_draft(tmp_path, monkeypatch):
    source = fixture_input(tmp_path)
    raw = canonical_bytes(analyze(source, tmp_path).model_dump())
    logs = Logs()
    logs.stdout.feed_data(
        json.dumps(
            {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "capabilities": {
                    "nonroot": True,
                    "no_inherited_secrets": True,
                    "readonly_root": True,
                    "readonly_input": True,
                    "network_denied": True,
                    "no_capabilities": True,
                },
            }
        ).encode()
        + b"\n"
    )

    async def launch(*args, **kwargs):
        return logs

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    runner = OwnedRunner("sha256:" + "a" * 64, tmp_path / "owner")

    async def transfer(name):
        logs.stderr.feed_data(b"x" * 65537)
        await asyncio.sleep(0)
        return raw

    runner._transfer = transfer
    with pytest.raises(AnalysisProcessError):
        await runner.analyze(source, tmp_path)
    assert runner.removed


async def test_cancellation_supervises_summary_reader(tmp_path, monkeypatch):
    source = fixture_input(tmp_path)
    logs, launched = Logs(), asyncio.Event()

    async def launch(*args, **kwargs):
        launched.set()
        return logs

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    runner = OwnedRunner("sha256:" + "a" * 64, tmp_path / "owner")
    before = asyncio.all_tasks()
    task = asyncio.create_task(runner.analyze(source, tmp_path))
    await launched.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    leaked = [t for t in asyncio.all_tasks() if t not in before and not t.done()]
    try:
        assert not leaked
    finally:
        for pending in leaked:
            pending.cancel()
        await asyncio.gather(*leaked, return_exceptions=True)


async def test_ownership_is_recorded_before_container_creation(tmp_path):
    source = fixture_input(tmp_path)
    runner = OwnedRunner("sha256:" + "a" * 64, tmp_path / "owner")

    async def command(*args, **kwargs):
        if args[0] == "create":
            assert list(runner.root.glob("*.container.json")), "container lacks recovery record"
            raise RuntimeError("injected create failure")
        return b""

    runner._command = command
    with pytest.raises(RuntimeError, match="injected create failure"):
        await runner.analyze(source, tmp_path)


async def test_restart_cleanup_checks_actual_marker_and_preserves_failed_record(tmp_path):
    from app.admission.process import AdmissionProcessFailed

    runner = DockerAnalysisRunner("sha256:" + "a" * 64, tmp_path / "owner")
    name = "codespy-analysis-" + "b" * 32
    path = runner.root / (name + ".container.json")
    path.write_text(json.dumps({"name": name, "marker": runner.marker, "image": runner.image}))
    restored = DockerAnalysisRunner.restore_owned(runner.root)
    commands = []

    async def foreign(*args, **kwargs):
        commands.append(args)
        return b'{"codespy.phase2.admission-marker":"foreign"}'

    restored._command = foreign
    with pytest.raises(AdmissionProcessFailed, match="ownership mismatch"):
        await restored.cleanup_pending()
    assert path.exists() and not any(c[0] == "rm" for c in commands)

    async def matching(*args, **kwargs):
        commands.append(args)
        return (
            json.dumps({"codespy.phase2.admission-marker": runner.marker}).encode()
            if args[0] == "inspect"
            else b""
        )

    restored._command = matching
    await restored.cleanup_pending()
    assert not path.exists() and any(c[0] == "rm" and c[-1] == name for c in commands)


async def test_transfer_output_flood_cannot_deadlock_full_pipes(tmp_path, monkeypatch):
    import sys

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(b'x'*(18*1024*1024)); "
        "sys.stderr.buffer.write(b'y'*100000)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    runner = OwnedRunner("sha256:" + "a" * 64, tmp_path / "owner")

    async def launch(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    try:
        with pytest.raises(AnalysisProcessError):
            async with asyncio.timeout(2):
                await runner._transfer("owned-fixture")
    finally:
        if process.returncode is None:
            process.kill()
        async with asyncio.timeout(3):
            await process.communicate()


async def test_qualification_fixture_requires_five_dialects(tmp_path):
    runner = OwnedRunner("sha256:" + "a" * 64, tmp_path / "owner")

    async def trusted_fixture(source, root):
        assert {f.path.split(".")[-1] for f in source.files} == {"py", "js", "jsx", "ts", "tsx"}
        return analyze(source, root)

    runner.analyze = trusted_fixture
    assert await runner.qualify_analysis() == "a" * 64
