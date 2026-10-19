"""Pinned Linux analyzer with bounded transfer, process ownership and cancellation."""

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from app.admission.process import DockerAdmissionRunner
from app.contracts.structure import SourceInputV1, StructuralDraftV1
from app.domain.canonical_json import canonical_bytes
from app.domain.errors import PersistenceError


class AnalysisProcessError(PersistenceError):
    pass


async def drain(stream: asyncio.StreamReader, maximum: int) -> bytes:
    result = bytearray()
    while block := await stream.read(65536):
        if len(result) + len(block) > maximum:
            raise AnalysisProcessError("analyzer_output_invalid")
        result.extend(block)
    return bytes(result)


class DockerAnalysisRunner(DockerAdmissionRunner):
    async def readiness(self) -> str:
        config = json.loads(
            await self._command("image", "inspect", "--format", "{{json .Config}}", self.image)
        )
        labels = config.get("Labels") or {}
        toolchain = labels.get("codespy.phase3.toolchain-sha256", "")
        if (
            labels.get("codespy.analysis") != "structural-small-v1"
            or config.get("Entrypoint") != ["python", "-m", "app.analyzers.entrypoint"]
            or len(toolchain) != 64
            or toolchain == "0" * 64
            or any(c not in "0123456789abcdef" for c in toolchain)
        ):
            raise AnalysisProcessError("analysis_unavailable")
        return str(toolchain)

    async def _transfer(self, name: str) -> bytes:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            name,
            "python",
            "-m",
            "app.analyzers.entrypoint",
            "export",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        try:
            async with asyncio.TaskGroup() as group:
                output = group.create_task(drain(process.stdout, 16 * 1024 * 1024))
                errors = group.create_task(drain(process.stderr, 65536))
                group.create_task(process.wait())
            if process.returncode or errors.result():
                raise AnalysisProcessError("analyzer_output_invalid")
            return output.result()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def analyze(self, source: SourceInputV1, root: Path) -> StructuralDraftV1:
        if source.toolchain_hash != await self.readiness():
            raise AnalysisProcessError("analysis_unavailable")
        request = root / "request.json"
        with request.open("xb") as stream:
            stream.write(canonical_bytes(source.model_dump()))
        for filename, cap in (
            ("archive.zip", 104857600),
            ("manifest.json", 67108864),
            ("request.json", 8 * 1024 * 1024),
        ):
            path = root / filename
            if path.is_symlink() or not path.is_file() or path.stat().st_size > cap:
                raise AnalysisProcessError("source_integrity_error")
            path.chmod(0o644)
        name = "codespy-analysis-" + uuid4().hex
        args = [
            "create",
            "--name",
            name,
            "--label",
            "codespy.phase2.admission-marker=" + self.marker,
            "--network",
            "none",
            "--read-only",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "1g",
            "--memory-swap",
            "1g",
            "--cpus",
            "2",
            "--pids-limit",
            "64",
            "--ulimit",
            "nofile=128:128",
            "--ulimit",
            "cpu=180:180",
            "--ulimit",
            "fsize=16777216:16777216",
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=64k",
            "--log-opt",
            "max-file=1",
            "--log-opt",
            "compress=false",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=33554432,mode=700,uid=65534,gid=65534",  # noqa: S108 - bounded container tmpfs
            "--tmpfs",
            "/output:rw,noexec,nosuid,size=16777216,mode=700,uid=65534,gid=65534",
        ]
        for filename in ("archive.zip", "manifest.json", "request.json"):
            args.extend(
                ["--mount", f"type=bind,src={root / filename},dst=/input/{filename},readonly"]
            )
        args.append(self.image)
        logs = None
        diagnostics = None
        try:
            async with asyncio.timeout(source.config.limits.analysis_seconds):
                await self._command(*args)
                controls = json.loads(
                    await self._command("inspect", "--format", "{{json .HostConfig}}", name)
                )
                if (
                    controls["NetworkMode"] != "none"
                    or not controls["ReadonlyRootfs"]
                    or controls["Privileged"]
                    or controls["Memory"] != 1073741824
                    or controls["MemorySwap"] != 1073741824
                    or controls["PidsLimit"] != 64
                    or controls["NanoCpus"] != 2000000000
                    or "ALL" not in controls["CapDrop"]
                ):
                    raise AnalysisProcessError("analysis_unavailable")
                await self._command("start", name)
                logs = await asyncio.create_subprocess_exec(
                    "docker",
                    "logs",
                    "--follow",
                    name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=65536,
                )
                assert logs.stdout is not None and logs.stderr is not None
                diagnostics = asyncio.create_task(drain(logs.stderr, 65536))
                summary_task = asyncio.create_task(logs.stdout.readline())
                done, _ = await asyncio.wait(
                    [diagnostics, summary_task], return_when=asyncio.FIRST_COMPLETED
                )
                if diagnostics in done:
                    await diagnostics
                    if not summary_task.done():
                        summary_task.cancel()
                        await asyncio.gather(summary_task, return_exceptions=True)
                        raise AnalysisProcessError("analyzer_output_invalid")
                summary = json.loads(await summary_task)
                raw = await self._transfer(name)
                if hashlib.sha256(raw).hexdigest() != summary.get("sha256"):
                    raise AnalysisProcessError("analyzer_output_invalid")
                return StructuralDraftV1.model_validate_json(raw)
        except TimeoutError:
            raise AnalysisProcessError("analysis_timeout") from None
        finally:
            if logs is not None and logs.returncode is None:
                logs.kill()
                await logs.wait()
            if diagnostics is not None:
                diagnostics.cancel()
                await asyncio.gather(diagnostics, return_exceptions=True)
            await self._remove(name)
