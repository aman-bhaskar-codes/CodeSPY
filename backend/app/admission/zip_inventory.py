"""Bounded ZIP32 inventory. Submitted code is never extracted, imported or executed."""

import hashlib
import io
import stat
import struct
import zipfile
from pathlib import PurePosixPath

from app.contracts.ingestion import (
    AdmissionPolicy,
    AdmissionPolicyV2,
    ArtifactManifestV1,
    ManifestEntryV1,
    Reason,
)
from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.security.archive_paths import UnsafeArchivePath, collision_key, validate_archive_path


class AdmissionRejected(ValueError):  # noqa: N818 -- stable admission error contract
    pass


LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".json": "json",
    ".md": "markdown",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".txt": "text",
}


def preflight(data: bytes, p: AdmissionPolicy) -> int:
    if (
        len(data) > p.max_archive_bytes
        or len(data) < 22
        or data[:4] not in {b"PK\x03\x04", b"PK\x05\x06"}
    ):
        raise AdmissionRejected("unsupported_format")
    tail = data[max(0, len(data) - 65557) :]
    relative = tail.rfind(b"PK\x05\x06")
    if relative < 0:
        raise AdmissionRejected("archive_corrupt")
    at = max(0, len(data) - 65557) + relative
    try:
        _, disk, start_disk, on_disk, total, size, offset, comment = struct.unpack_from(
            "<4s4H2IH", data, at
        )
    except struct.error:
        raise AdmissionRejected("archive_corrupt") from None
    if (
        disk
        or start_disk
        or on_disk != total
        or total == 65535
        or size == 0xFFFFFFFF
        or offset == 0xFFFFFFFF
    ):
        raise AdmissionRejected("unsupported_format")
    if total > p.max_entries or size > p.max_directory_bytes:
        raise AdmissionRejected("archive_limit_exceeded")
    if offset + size != at or at + 22 + comment != len(data):
        raise AdmissionRejected("archive_corrupt")
    pos = offset
    for _ in range(total):
        if data[pos : pos + 4] != b"PK\x01\x02" or pos + 46 > at:
            raise AdmissionRejected("archive_corrupt")
        flags, method = struct.unpack_from("<HH", data, pos + 8)
        compressed, expanded = struct.unpack_from("<II", data, pos + 20)
        name_len, extra_len, comment_len, disk_start = struct.unpack_from("<4H", data, pos + 28)
        if (
            flags & 1
            or method not in {0, 8}
            or disk_start
            or compressed == 0xFFFFFFFF
            or expanded == 0xFFFFFFFF
        ):
            raise AdmissionRejected("unsupported_format")
        end = pos + 46 + name_len + extra_len + comment_len
        if end > at:
            raise AdmissionRejected("archive_corrupt")
        raw = data[pos + 46 : pos + 46 + name_len]
        if b"\0" in raw:
            raise AdmissionRejected("unsafe_archive_path")
        try:
            raw.decode("utf-8" if flags & 0x800 else "ascii")
        except UnicodeError:
            raise AdmissionRejected("unsupported_name_encoding") from None
        extra = data[pos + 46 + name_len : pos + 46 + name_len + extra_len]
        ep = 0
        while ep < len(extra):
            if ep + 4 > len(extra):
                raise AdmissionRejected("archive_corrupt")
            kind, length = struct.unpack_from("<HH", extra, ep)
            if kind == 1:
                raise AdmissionRejected("unsupported_format")
            ep += 4 + length
            if ep > len(extra):
                raise AdmissionRejected("archive_corrupt")
        pos = end
    if pos != at:
        raise AdmissionRejected("archive_corrupt")
    return int(total)


def inspect_zip_bytes(
    data: bytes,
    policy: AdmissionPolicy,
    *,
    source_revision: str | None = None,
    github_repository_id: int | None = None,
) -> ArtifactManifestV1:
    total = preflight(data, policy)
    entries = []
    seen: dict[str, bool] = {}
    ancestors: set[str] = set()
    expanded = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            names = [
                validate_archive_path(
                    i.filename, max_bytes=policy.max_path_bytes, max_depth=policy.max_depth
                )
                for i in infos
            ]
            root = None
            if policy.github_strip_root and names:
                roots = {n.split("/")[0] for n in names}
                if len(roots) != 1:
                    raise AdmissionRejected("unsafe_archive_path")
                root = next(iter(roots))
            for info, name in zip(infos, names, strict=True):
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise AdmissionRejected("unsafe_archive_path")
                directory = info.is_dir()
                if root:
                    if name == root:
                        if not directory:
                            raise AdmissionRejected("unsafe_archive_path")
                        continue
                    name = name[len(root) + 1 :]
                    validate_archive_path(
                        name, max_bytes=policy.max_path_bytes, max_depth=policy.max_depth
                    )
                key = collision_key(name)
                if key in seen:
                    raise AdmissionRejected("archive_collision")
                for parent in PurePosixPath(key).parents:
                    if str(parent) != "." and seen.get(str(parent)) is False:
                        raise AdmissionRejected("archive_collision")
                if not directory and key in ancestors:
                    raise AdmissionRejected("archive_collision")
                ancestors.update(str(p) for p in PurePosixPath(key).parents if str(p) != ".")
                seen[key] = directory
                if directory:
                    continue
                digest = hashlib.sha256()
                size = 0
                prefix = b""
                text_valid = True
                import codecs

                decoder = codecs.getincrementaldecoder("utf-8")()
                with z.open(info) as stream:
                    while block := stream.read(65536):
                        size += len(block)
                        expanded += len(block)
                        if (
                            size > policy.max_file_bytes
                            or expanded > policy.max_expanded_bytes
                            or expanded > len(data) * policy.max_ratio
                        ):
                            raise AdmissionRejected("archive_limit_exceeded")
                        digest.update(block)
                        if len(prefix) < 4096:
                            prefix = (prefix + block)[:4096]
                        if b"\0" in block:
                            text_valid = False
                        if text_valid:
                            try:
                                decoder.decode(block)
                            except UnicodeError:
                                text_valid = False
                if text_valid:
                    try:
                        decoder.decode(b"", final=True)
                    except UnicodeError:
                        text_valid = False
                if size != info.file_size:
                    raise AdmissionRejected("archive_corrupt")
                path = PurePosixPath(name)
                parts = {x.casefold() for x in path.parts}
                language = LANGUAGES.get(path.suffix.casefold())
                lock_metadata = isinstance(policy, AdmissionPolicyV2) and path.name == "uv.lock"
                if lock_metadata:
                    language = "toml"
                reason: Reason | None = None
                if path.name.casefold() in {
                    ".env",
                    "id_rsa",
                    "id_ed25519",
                    "credentials",
                } or path.suffix.casefold() in {".pem", ".key"}:
                    reason = "sensitive_path"
                elif parts & {".git", "node_modules", "vendor", "build", "dist", "__pycache__"}:
                    reason = "generated_dependency"
                elif path.suffix.casefold() in {".zip", ".tar", ".gz", ".asar", ".jar"}:
                    reason = "nested_archive"
                elif size > policy.eligible_file_bytes or (
                    isinstance(policy, AdmissionPolicyV2)
                    and lock_metadata
                    and size > policy.metadata_file_bytes
                ):
                    reason = "oversized"
                elif b"\0" in prefix or not text_valid:
                    reason = "binary"
                elif language is None:
                    reason = "unsupported_language"
                entries.append(
                    ManifestEntryV1(
                        path=name,
                        size_bytes=size,
                        sha256=digest.hexdigest(),
                        language_hint=language,
                        eligible=reason is None,
                        reason=reason,
                    )
                )
    except AdmissionRejected:
        raise
    except UnsafeArchivePath:
        raise AdmissionRejected("unsafe_archive_path") from None
    except (zipfile.BadZipFile, RuntimeError, ValueError, EOFError, struct.error):
        raise AdmissionRejected("archive_corrupt") from None
    entries.sort(key=lambda e: e.path.encode())
    normalized = [
        {
            "path": e.path,
            "sha256": e.sha256,
            "size_bytes": e.size_bytes,
            "language": e.language_hint,
        }
        for e in entries
    ]
    result = ArtifactManifestV1(
        policy_version=policy.version,
        policy_hash=policy.digest(),
        archive_sha256=hashlib.sha256(data).hexdigest(),
        archive_size=len(data),
        normalized_files_sha256=semantic_digest(normalized),
        source_revision=source_revision,
        github_repository_id=github_repository_id,
        entries=entries,
        total_entries=total,
    )
    if len(canonical_bytes(result.model_dump())) > policy.max_manifest_bytes:
        raise AdmissionRejected("archive_limit_exceeded")
    return result
