"""Archive parsing kernel; invoke only inside the source reader sandbox."""

import hashlib
import io
import stat
import zipfile

from app.admission.zip_inventory import AdmissionRejected, preflight
from app.contracts.ingestion import AdmissionPolicyV1
from app.security.archive_paths import collision_key, validate_archive_path

MAX_FILE_BYTES = 2_097_152


class SourceReadRejected(ValueError):  # noqa: N818 - stable domain rejection name
    pass


def read_admitted_entry(
    raw: bytes, *, path: str, sha256: str, size_bytes: int, strip_root: bool
) -> bytes:
    """Verify a complete bounded entry before any downstream range/privacy transform."""
    try:
        validate_archive_path(path)
        if not 0 <= size_bytes <= MAX_FILE_BYTES:
            raise SourceReadRejected("source_read_rejected")
        preflight(raw, AdmissionPolicyV1())
        with zipfile.ZipFile(io.BytesIO(raw)) as source:
            paths: dict[str, zipfile.ZipInfo] = {}
            collisions: set[str] = set()
            roots: set[str] = set()
            for info in source.infolist():
                name = validate_archive_path(info.filename)
                roots.add(name.split("/")[0])
                if info.is_dir():
                    continue
                if strip_root:
                    if "/" not in name:
                        raise SourceReadRejected("source_read_rejected")
                    name = name.split("/", 1)[1]
                    validate_archive_path(name)
                key = collision_key(name)
                mode = info.external_attr >> 16
                if key in collisions or stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                    raise SourceReadRejected("source_read_rejected")
                collisions.add(key)
                paths[name] = info
            if strip_root and len(roots) != 1:
                raise SourceReadRejected("source_read_rejected")
            selected = paths.get(path)
            if (
                selected is None
                or selected.file_size != size_bytes
                or selected.flag_bits & 1
                or selected.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            ):
                raise SourceReadRejected("source_read_rejected")
            with source.open(selected) as entry:
                content = entry.read(size_bytes + 1)
                if len(content) != size_bytes or entry.read(1):
                    raise SourceReadRejected("source_read_rejected")
            if hashlib.sha256(content).hexdigest() != sha256:
                raise SourceReadRejected("source_read_rejected")
            return content
    except (
        AdmissionRejected,
        ValueError,
        KeyError,
        OSError,
        zipfile.BadZipFile,
        NotImplementedError,
        RuntimeError,
    ) as exc:
        raise SourceReadRejected("source_read_rejected") from exc
