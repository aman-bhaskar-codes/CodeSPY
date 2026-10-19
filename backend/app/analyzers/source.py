"""Read verified admitted entries inside isolation, never extract their paths."""

import hashlib
import io
import os
import stat
import zipfile
from pathlib import Path

from app.admission.zip_inventory import inspect_zip_bytes
from app.contracts.ingestion import ArtifactManifestV1
from app.contracts.structure import Language, SourceInputV1, SourceUnitV1
from app.domain.errors import PersistenceError


class SourceIntegrityError(PersistenceError):
    pass


DIALECTS: dict[str, Language] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "jsx",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
}


class AdmittedSourceReader:
    def __init__(self, input: SourceInputV1, root: Path):
        self.input = input
        self.root = root
        self.omitted: list[tuple[str, str]] = []
        self.configs: list[dict[str, str]] = []
        self.requested = 0

    def _read(self, filename: str, maximum: int) -> bytes:
        if filename not in {"archive.zip", "manifest.json"}:
            raise SourceIntegrityError("source_integrity_error")
        fd = os.open(self.root / filename, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise SourceIntegrityError("source_integrity_error")
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise SourceIntegrityError("source_integrity_error")
        return data

    def read_selected(self) -> tuple[SourceUnitV1, ...]:
        expected = self.input
        archive = self._read(expected.archive.path, 104857600)
        raw_manifest = self._read(expected.manifest.path, 67108864)
        for raw, descriptor in [(archive, expected.archive), (raw_manifest, expected.manifest)]:
            if (
                len(raw) != descriptor.size_bytes
                or hashlib.sha256(raw).hexdigest() != descriptor.sha256
            ):
                raise SourceIntegrityError("source_integrity_error")
        manifest = ArtifactManifestV1.model_validate_json(raw_manifest)
        actual = inspect_zip_bytes(
            archive,
            expected.policy.admission,
            source_revision=manifest.source_revision,
            github_repository_id=manifest.github_repository_id,
        )
        if actual != manifest or actual.policy_hash != expected.policy.policy_hash:
            raise SourceIntegrityError("source_integrity_error")
        files = {file.path: file for file in expected.files}
        if set(files) != {entry.path for entry in manifest.entries}:
            raise SourceIntegrityError("source_integrity_error")
        for entry in manifest.entries:
            stored = files[entry.path]
            if (
                stored.sha256,
                stored.size_bytes,
                stored.eligible,
                stored.exclusion_reason,
                stored.language,
            ) != (
                entry.sha256,
                entry.size_bytes,
                entry.eligible,
                entry.reason,
                entry.language_hint,
            ):
                raise SourceIntegrityError("source_integrity_error")
        units: list[SourceUnitV1] = []
        total = config_bytes = 0
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            paths = {}
            for info in source.infolist():
                if info.is_dir():
                    continue
                path = (
                    info.filename.split("/", 1)[1]
                    if expected.policy.strip_single_root
                    else info.filename
                )
                paths[path] = info
            for path in sorted(files, key=lambda p: p.encode()):
                stored = files[path]
                in_scope = not expected.config.include_prefixes or any(
                    path == p or path.startswith(p + "/") for p in expected.config.include_prefixes
                )
                if not stored.eligible or not in_scope:
                    continue
                language = DIALECTS.get(Path(path).suffix.casefold())
                is_config = Path(path).name == "package.json" or (
                    Path(path).name.startswith("tsconfig") and path.endswith((".json", ".jsonc"))
                )
                if language:
                    self.requested += 1
                    if (
                        len(units) >= expected.config.limits.source_files
                        or total + stored.size_bytes > expected.config.limits.source_bytes
                    ):
                        self.omitted.append((path, "source_capacity"))
                        continue
                elif not is_config:
                    continue
                if is_config and (
                    len(self.configs) >= expected.config.limits.config_files
                    or stored.size_bytes > expected.config.limits.config_file_bytes
                    or config_bytes + stored.size_bytes > expected.config.limits.config_bytes
                ):
                    continue
                if stored.size_bytes > expected.config.limits.file_bytes:
                    raise SourceIntegrityError("source_integrity_error")
                data = bytearray()
                with source.open(paths[path]) as stream:
                    while block := stream.read(65536):
                        if len(data) + len(block) > stored.size_bytes:
                            raise SourceIntegrityError("source_integrity_error")
                        data.extend(block)
                raw = bytes(data)
                if (
                    len(raw) != stored.size_bytes
                    or hashlib.sha256(raw).hexdigest() != stored.sha256
                ):
                    raise SourceIntegrityError("source_integrity_error")
                if language:
                    units.append(
                        SourceUnitV1(
                            file_id=stored.file_id,
                            path=path,
                            sha256=stored.sha256,
                            data=raw,
                            language=language,
                        )
                    )
                    total += len(raw)
                else:
                    self.configs.append({"path": path, "text": raw.decode("utf-8")})
                    config_bytes += len(raw)
        return tuple(units)
