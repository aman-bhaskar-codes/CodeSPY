"""Private scanner container entrypoint. Only normalized metadata reaches stdout."""

import asyncio
import hashlib
import os
import resource
import stat
import sys
import tempfile
from pathlib import Path
from uuid import UUID

from app.analyzers.positions import SourceIndex
from app.contracts.scanners import (
    ScannerArchiveV1,
    ScannerObservationV1,
    ScannerRequestV1,
    ScannerResultV1,
)
from app.scanners.attestation import ScannerAttestationV1, attest
from app.scanners.commands import gitleaks_command, semgrep_command
from app.scanners.dependency_worker import DependencyInputV1, assess_dependencies
from app.scanners.normalization import ScannerOutputError, normalize_gitleaks, normalize_semgrep
from app.scanners.process import NativeProcessError, run_private
from app.scanners.runtime import RuntimeManifest, load_manifest
from app.scanners.source import read_scanner_sources


def _read(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ScannerOutputError()
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ScannerOutputError()
    return raw


def read_sources(
    request: ScannerRequestV1, root: Path
) -> tuple[dict[UUID, bytes], dict[str, UUID]]:
    sources, aliases = {}, {}
    suffix = ".py" if request.profile.tool == "semgrep" else ".txt"
    try:
        for file in request.files:
            alias = file.file_id.hex + suffix
            raw = _read(root / alias, file.size_bytes)
            if len(raw) != file.size_bytes or hashlib.sha256(raw).hexdigest() != file.sha256:
                raise ScannerOutputError()
            SourceIndex(raw)
            sources[file.file_id], aliases[alias] = raw, file.file_id
    except (OSError, ValueError):
        raise ScannerOutputError() from None
    return sources, aliases


async def scan(
    request: ScannerRequestV1, root: Path, *, manifest: RuntimeManifest, source: ScannerArchiveV1
) -> ScannerResultV1:
    """Called only within the offline nonroot container; no broker credentials."""
    runtime = manifest.select(request.profile)
    if source.request != request:
        raise ScannerOutputError()
    sources = read_scanner_sources(source, root)
    suffix = ".py" if request.profile.tool == "semgrep" else ".txt"
    aliases = {file.file_id.hex + suffix: file.file_id for file in request.files}
    observations: list[ScannerObservationV1] = []
    async with asyncio.timeout(request.profile.deadline_seconds):
        with tempfile.TemporaryDirectory(prefix="scanner-") as temporary:
            owned = Path(temporary)
            for alias, file_id in aliases.items():
                with (owned / alias).open("xb") as stream:
                    stream.write(sources[file_id])
                (owned / alias).chmod(0o400)
            read_sources(request, owned)
            if not request.files:
                return ScannerResultV1(
                    request_digest=request.semantic_hash(),
                    status="completed",
                    processed_file_ids=(),
                    omitted_file_ids=(),
                )
            if request.profile.tool == "semgrep":
                native = await run_private(
                    semgrep_command(tuple(aliases), runtime=runtime),
                    cwd=owned,
                    deadline=180,
                    stdout_limit=request.profile.output_bytes,
                    stderr_limit=65536,
                )
                return normalize_semgrep(
                    request,
                    sources,
                    aliases,
                    native.output,
                    exit_code=native.exit_code,
                    expected_version=runtime.version,
                )
            output_size = 0
            for alias, file_id in aliases.items():
                file = next(file for file in request.files if file.file_id == file_id)
                selected = request.model_copy(update={"files": (file,)})
                native = await run_private(
                    gitleaks_command(alias, runtime=runtime),
                    cwd=owned,
                    deadline=20,
                    stdout_limit=request.profile.output_bytes,
                    stderr_limit=65536,
                )
                if native.diagnostic_bytes:
                    raise ScannerOutputError()
                output_size += len(native.output)
                if output_size > request.profile.output_bytes:
                    raise ScannerOutputError()
                result = normalize_gitleaks(
                    selected,
                    {file_id: sources[file_id]},
                    {alias: file_id},
                    native.output,
                    exit_code=native.exit_code,
                )
                if result.status != "completed":
                    raise ScannerOutputError()
                observations.extend(result.observations)
                if len(observations) > 1000:
                    raise ScannerOutputError()
    result = ScannerResultV1(
        request_digest=request.semantic_hash(),
        status="completed",
        processed_file_ids=tuple(file.file_id for file in request.files),
        omitted_file_ids=(),
        observations=tuple(observations),
    )
    result.validate_for(request)
    return result


async def verify_versions(manifest: RuntimeManifest) -> None:
    with tempfile.TemporaryDirectory(prefix="scanner-readiness-") as private:
        for tool in manifest.tools:
            flag = "--version" if tool.tool == "semgrep" else "version"
            result = await run_private(
                (str(tool.executable), flag),
                deadline=20,
                stdout_limit=1024,
                stderr_limit=65536,
                cwd=Path(private),
            )
            if result.exit_code or result.output.strip() != tool.version.encode("ascii"):
                raise ScannerOutputError()


def main() -> int:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2097152, 2097152))
    try:
        if len(sys.argv) not in {3, 4} or sys.argv[1] != "--runtime":
            return 64
        manifest = load_manifest(Path(sys.argv[2]))
        if sys.argv[3:] == ["dependencies"]:
            dependency = DependencyInputV1.model_validate_json(
                _read(Path("/input/request.json"), 16777216)
            )
            sys.stdout.write(assess_dependencies(dependency, Path("/input")).model_dump_json())
            return 0
        if sys.argv[3:] == ["identities"]:
            asyncio.run(verify_versions(manifest))
            print(manifest.model_dump_json())
            return 0
        if sys.argv[3:] == ["attest"]:
            value = ScannerAttestationV1.model_validate_json(
                _read(Path("/input/request.json"), 10485760)
            )
            sys.stdout.write(attest(value, Path("/input")))
            return 0
        if sys.argv[3:]:
            return 64
        source = ScannerArchiveV1.model_validate_json(_read(Path("/input/request.json"), 8388608))
        request = source.request
    except Exception:
        return 65
    try:
        result = asyncio.run(scan(request, Path("/input"), manifest=manifest, source=source))
    except (NativeProcessError, TimeoutError) as error:
        if isinstance(error, TimeoutError) or str(error) == "scanner_timeout":
            return 75
        return 65
    except Exception:
        result = ScannerResultV1(
            request_digest=request.semantic_hash(),
            status="failed",
            processed_file_ids=(),
            omitted_file_ids=tuple(file.file_id for file in request.files),
        )
    sys.stdout.buffer.write(result.model_dump_json().encode())
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
