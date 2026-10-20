"""Owned Linux admission containers: fixed command, no network or inherited secrets."""

import asyncio
import hashlib
import json
import os
import re
import shutil
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from app.adapters.storage.local import LocalObjectStore
from app.admission.zip_inventory import AdmissionRejected
from app.contracts.ingestion import AdmissionPolicy, ArtifactManifestV1, ObjectDescriptor
from app.domain.canonical_json import canonical_bytes
from app.domain.errors import DuplicateConflict, PersistenceError


class AdmissionProcessFailed(PersistenceError):  # noqa: N818 - established domain name
    pass


class DockerAdmissionRunner:
    def __init__(self, image: str, root: Path):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("pinned owned image ID required")
        self.image = image
        self.root = root.absolute()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError("private sandbox workspace required")
        self.marker = uuid4().hex
        marker = self.root / "ownership.json"
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"marker": self.marker, "image": image}, stream)

    async def _command(self, *args: str, check: bool = True) -> bytes:
        process = await asyncio.create_subprocess_exec(
            "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            async with asyncio.timeout(15):
                out, error = await process.communicate()
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if len(out) + len(error) > 65536 or (check and process.returncode != 0):
            raise AdmissionProcessFailed("sandbox control operation failed")
        return out

    async def _remove(self, name: str) -> None:
        raw = await self._command(
            "inspect", "--format", "{{json .Config.Labels}}", name, check=False
        )
        if not raw.strip():
            return
        labels = json.loads(raw)
        if labels.get("codespy.phase2.admission-marker") != self.marker:
            raise AdmissionProcessFailed("sandbox ownership mismatch")
        await self._command("rm", "--force", name)

    async def _output(self, name: str, filename: str) -> bytes:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            name,
            "python",
            "-m",
            "app.admission.entrypoint",
            "export_manifest" if filename == "manifest.json" else "export_probe",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        data = bytearray()
        try:
            assert process.stdout is not None
            while block := await process.stdout.read(65536):
                data.extend(block)
                if len(data) > 67108864:
                    raise AdmissionProcessFailed("sandbox transfer exceeds output bound")
            assert process.stderr is not None
            error = await process.stderr.read(65537)
            await process.wait()
            if process.returncode != 0 or len(error) > 65536:
                raise AdmissionProcessFailed("sandbox output unavailable")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        return bytes(data)

    async def _launch(self, directory: Path, mode: Literal["inspect", "qualify"]) -> dict[str, Any]:
        metadata = json.loads(
            await self._command("image", "inspect", "--format", "{{json .Config}}", self.image)
        )
        if metadata.get("Labels", {}).get("codespy.admission") != "source-zip-v1" or metadata.get(
            "Entrypoint"
        ) != ["python", "-m", "app.admission.entrypoint"]:
            raise AdmissionProcessFailed("unqualified admission image")
        name = "codespy-p2-admission-" + self.marker[:8] + "-" + uuid4().hex[:12]
        ownership = directory / "container.json"
        ownership.write_text(json.dumps({"name": name, "marker": self.marker}))
        source = directory / "source.zip"
        request = directory / "request.json"
        source.chmod(0o644)
        request.chmod(0o644)
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
            "512m",
            "--memory-swap",
            "512m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--ulimit",
            "cpu=60:60",
            "--ulimit",
            "fsize=67108864:67108864",
            "--ulimit",
            "nofile=128:128",
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=64k",
            "--log-opt",
            "max-file=1",
            "--log-opt",
            "compress=false",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16777216,mode=700,uid=65534,gid=65534",  # noqa: S108 - private bounded container tmpfs
            "--tmpfs",
            "/output:rw,noexec,nosuid,size=67108864,mode=700,uid=65534,gid=65534",
            "--mount",
            f"type=bind,src={source},dst=/input/source.zip,readonly",
            "--mount",
            f"type=bind,src={request},dst=/input/request.json,readonly",
            self.image,
            mode,
        ]
        try:
            await self._command(*args)
            config = json.loads(
                await self._command("inspect", "--format", "{{json .HostConfig}}", name)
            )
            if (
                config["NetworkMode"] != "none"
                or not config["ReadonlyRootfs"]
                or config["Privileged"]
                or config["Memory"] != 536870912
                or config["PidsLimit"] != 64
                or "ALL" not in config["CapDrop"]
            ):
                raise AdmissionProcessFailed("sandbox controls unavailable")
            started = asyncio.get_running_loop().time()
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

            async def diagnostics() -> bytes:
                assert logs.stderr is not None
                result = bytearray()
                while block := await logs.stderr.read(4096):
                    result.extend(block)
                    if len(result) > 65536:
                        raise AdmissionProcessFailed("sandbox diagnostics exceed bound")
                return bytes(result)

            errors = asyncio.create_task(diagnostics())
            try:
                async with asyncio.timeout_at(started + 120):
                    assert logs.stdout is not None
                    line = await logs.stdout.readline()
                    if not line or len(line) > 65536:
                        raise AdmissionProcessFailed("invalid sandbox response")
                    try:
                        summary = json.loads(line)
                    except (ValueError, UnicodeError):
                        raise AdmissionProcessFailed("invalid sandbox response") from None
                    if summary.get("status") != "ok":
                        if summary.get("status") == "rejected" and summary.get("code") in {
                            "unsupported_format",
                            "unsupported_name_encoding",
                            "unsafe_archive_path",
                            "archive_collision",
                            "archive_corrupt",
                            "archive_limit_exceeded",
                        }:
                            raise AdmissionRejected(summary["code"])
                        raise AdmissionProcessFailed("admission process failed")
                    state = json.loads(
                        await self._command("inspect", "--format", "{{json .State}}", name)
                    )
                    if not state["Running"] or state.get("OOMKilled"):
                        raise AdmissionProcessFailed("sandbox stopped before output transfer")
                    raw = await self._output(
                        name, "manifest.json" if mode == "inspect" else "probe.json"
                    )
                    if hashlib.sha256(raw).hexdigest() != summary.get("sha256"):
                        raise AdmissionProcessFailed("sandbox output digest differs")
                    return {"raw": raw, "rss_kib": summary.get("rss_kib")}
            finally:
                if logs.returncode is None:
                    logs.kill()
                    await logs.wait()
                if not errors.done():
                    errors.cancel()
                await asyncio.gather(errors, return_exceptions=True)
        except TimeoutError:
            raise AdmissionProcessFailed("admission_timeout") from None
        finally:
            await self._remove(name)
            ownership.unlink(missing_ok=True)

    async def inspect_stream(
        self,
        chunks: AsyncIterable[bytes],
        expected: ObjectDescriptor,
        policy: AdmissionPolicy,
        *,
        source_revision: str | None = None,
        github_repository_id: int | None = None,
    ) -> ArtifactManifestV1:
        directory = self.root / uuid4().hex
        directory.mkdir(mode=0o700)
        try:
            store = LocalObjectStore(directory / "spool")
            staged = await store.put_stage("stage/source", chunks, policy.max_archive_bytes)
            if staged.sha256 != expected.sha256 or staged.size_bytes != expected.size_bytes:
                raise DuplicateConflict("selected input descriptor differs")
            os.link(store.root / staged.key, directory / "source.zip", follow_symlinks=False)
            (directory / "request.json").write_bytes(
                canonical_bytes(
                    {
                        "policy": policy.model_dump(),
                        "sha256": expected.sha256,
                        "size_bytes": expected.size_bytes,
                        "source_revision": source_revision,
                        "github_repository_id": github_repository_id,
                    }
                )
            )
            result = await self._launch(directory, "inspect")
            manifest = ArtifactManifestV1.model_validate_json(result["raw"])
            if (
                manifest.archive_sha256 != expected.sha256
                or manifest.archive_size != expected.size_bytes
                or manifest.policy_hash != policy.digest()
            ):
                raise AdmissionProcessFailed("sandbox manifest identity differs")
            return manifest
        finally:
            if not (directory / "container.json").exists():
                shutil.rmtree(directory)

    async def inspect_bytes(self, value: bytes, policy: AdmissionPolicy) -> ArtifactManifestV1:
        async def chunks() -> AsyncIterator[bytes]:
            yield value

        return await self.inspect_stream(
            chunks(),
            ObjectDescriptor(
                key="stage/fixture", sha256=hashlib.sha256(value).hexdigest(), size_bytes=len(value)
            ),
            policy,
        )

    async def qualify(self) -> dict[str, bool]:
        directory = self.root / uuid4().hex
        directory.mkdir(mode=0o700)
        try:
            (directory / "source.zip").write_bytes(b"")
            (directory / "request.json").write_bytes(b"{}")
            result = await self._launch(directory, "qualify")
            data = json.loads(result["raw"])
            if not data or not all(type(v) is bool and v for v in data.values()):
                raise AdmissionProcessFailed("sandbox capability qualification failed")
            return dict(data)
        finally:
            if not (directory / "container.json").exists():
                shutil.rmtree(directory)
