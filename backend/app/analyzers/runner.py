"""Pinned Linux analyzer with bounded transfer, process ownership and cancellation."""

import asyncio
import hashlib
import json
import os
import re
import sys
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
        output = asyncio.create_task(drain(process.stdout, 16 * 1024 * 1024))
        errors = asyncio.create_task(drain(process.stderr, 65536))
        waiter = asyncio.create_task(process.wait())
        tasks = [output, errors, waiter]
        try:
            await asyncio.gather(*tasks)
            if process.returncode or errors.result():
                raise AnalysisProcessError("analyzer_output_invalid")
            return output.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if process.returncode is None:
                process.kill()

            async def discard(stream: asyncio.StreamReader) -> None:
                while await stream.read(65536):
                    pass

            async with asyncio.timeout(5):
                await asyncio.gather(
                    discard(process.stdout), discard(process.stderr), process.wait()
                )

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
        record = self.root / (name + ".container.json")
        with record.open("x") as ownership:
            json.dump({"name": name, "marker": self.marker, "image": self.image}, ownership)
            ownership.flush()
            os.fsync(ownership.fileno())
        record.chmod(0o600)
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
        diagnostics = summary_task = tail = None
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
                line = await summary_task
                tail = asyncio.create_task(drain(logs.stdout, 65536 - len(line)))
                summary = json.loads(line)
                raw = await self._transfer(name)
                await asyncio.sleep(0)  # Let already-received diagnostics enforce their bound.
                for reader in (diagnostics, tail):
                    if reader.done():
                        reader.result()
                if hashlib.sha256(raw).hexdigest() != summary.get("sha256"):
                    raise AnalysisProcessError("analyzer_output_invalid")
                capabilities = summary.get("capabilities")
                expected = {
                    "nonroot",
                    "no_inherited_secrets",
                    "readonly_root",
                    "readonly_input",
                    "network_denied",
                    "no_capabilities",
                }
                if (
                    not isinstance(capabilities, dict)
                    or set(capabilities) != expected
                    or not all(value is True for value in capabilities.values())
                ):
                    raise AnalysisProcessError("analysis_unavailable")
                self.last_metrics = {
                    "rss_kib": summary.get("rss_kib"),
                    "cgroup_peak_bytes": summary.get("cgroup_peak_bytes"),
                }
                return StructuralDraftV1.model_validate_json(raw)
        except TimeoutError:
            raise AnalysisProcessError("analysis_timeout") from None
        finally:
            primary = sys.exc_info()[1]
            readers = [task for task in (diagnostics, summary_task, tail) if task is not None]
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            outcomes = await asyncio.gather(*readers, return_exceptions=True)
            try:
                if logs is not None:
                    if logs.returncode is None:
                        logs.kill()

                    # Drain/discard after kill so a full pipe cannot deadlock wait().
                    async def discard(stream: asyncio.StreamReader | None) -> None:
                        if stream is not None:
                            while await stream.read(65536):
                                pass

                    try:
                        async with asyncio.timeout(1):
                            await asyncio.gather(
                                discard(logs.stdout), discard(logs.stderr), logs.wait()
                            )
                    except TimeoutError:
                        if logs.returncode is None:
                            raise AnalysisProcessError("analyzer_cleanup_pending") from None
            finally:
                # A failed removal preserves the durable record for exact-owner recovery.
                await self._remove(name)
                record.unlink(missing_ok=True)
            if primary is None:
                for outcome in outcomes:
                    if isinstance(outcome, BaseException) and not isinstance(
                        outcome, asyncio.CancelledError
                    ):
                        raise outcome

    @classmethod
    def restore_owned(cls, root: Path) -> "DockerAnalysisRunner":
        root = root.absolute()
        if root.is_symlink() or (root / "ownership.json").is_symlink():
            raise ValueError("owned workspace required")
        raw = (root / "ownership.json").read_bytes()
        if len(raw) > 1024:
            raise ValueError("invalid owner record")
        owner = json.loads(raw)
        if not re.fullmatch(r"[0-9a-f]{32}", owner.get("marker", "")) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", owner.get("image", "")
        ):
            raise ValueError("invalid owner record")
        instance = cls.__new__(cls)
        instance.root, instance.marker, instance.image = root, owner["marker"], owner["image"]
        return instance

    async def cleanup_pending(self) -> None:
        for path in sorted(self.root.glob("*.container.json")):
            if path.is_symlink() or path.stat().st_size > 1024:
                raise ValueError("invalid container owner record")
            record = json.loads(path.read_bytes())
            name = record.get("name", "")
            if (
                not re.fullmatch(r"codespy-analysis-[0-9a-f]{32}", name)
                or path.name != name + ".container.json"
                or record.get("marker") != self.marker
                or record.get("image") != self.image
            ):
                raise ValueError("foreign container owner record")
            await self._remove(name)  # Also verifies the actual Docker ownership label.
            path.unlink()

    async def qualify_analysis(self) -> str:
        """Owned, trusted five-dialect smoke fixture before enabling local submissions."""
        import io
        import tempfile
        import zipfile

        from app.admission.zip_inventory import inspect_zip_bytes
        from app.contracts.ingestion import AdmissionPolicyV1
        from app.contracts.structure import (
            AnalysisConfigV1,
            SourceDescriptorV1,
            SourceFileV1,
            SourcePolicyV1,
        )

        toolchain = await self.readiness()
        files = {"main.py": b"def answer():\n return 42\nanswer()\n"}
        for suffix in ("js", "jsx", "ts", "tsx"):
            returned = "<div/>" if suffix in {"jsx", "tsx"} else "42"
            files["main." + suffix] = (
                f"export function answer(){{return {returned};}} answer();".encode()
            )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        raw = buffer.getvalue()
        policy = AdmissionPolicyV1()
        manifest = inspect_zip_bytes(raw, policy)
        encoded_manifest = manifest.model_dump_json().encode()
        config = AnalysisConfigV1(toolchain_hash=toolchain)
        source = SourceInputV1(
            archive=SourceDescriptorV1(
                path="archive.zip", sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
            ),
            manifest=SourceDescriptorV1(
                path="manifest.json",
                sha256=hashlib.sha256(encoded_manifest).hexdigest(),
                size_bytes=len(encoded_manifest),
            ),
            policy=SourcePolicyV1(policy_hash=policy.digest(), admission=policy),
            files=tuple(
                SourceFileV1(
                    file_id=uuid4(),
                    path=e.path,
                    sha256=e.sha256,
                    size_bytes=e.size_bytes,
                    language=e.language_hint,
                    eligible=e.eligible,
                    exclusion_reason=e.reason,
                )
                for e in manifest.entries
            ),
            config=config,
            config_hash=config.semantic_hash(),
            toolchain_hash=toolchain,
            request_nonce="owned-qualification",
        )
        with tempfile.TemporaryDirectory(prefix="qualify-", dir=self.root) as directory:
            root = Path(directory)
            (root / "archive.zip").write_bytes(raw)
            (root / "manifest.json").write_bytes(encoded_manifest)
            draft = await self.analyze(source, root)
            if draft.coverage.processed_files != 5 or draft.coverage.resolved < 5:
                raise AnalysisProcessError("analysis_unavailable")
        return toolchain
