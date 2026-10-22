"""Read only persisted, selected identities from authenticated admitted bytes."""

import hashlib
import io
import zipfile
from pathlib import Path
from uuid import UUID

from app.analyzers.positions import SourceIndex
from app.analyzers.source import SourceIntegrityError, authenticate_archive
from app.contracts.scanners import ScannerArchiveV1


def read_scanner_sources(source: ScannerArchiveV1, root: Path) -> dict[UUID, bytes]:
    raw = authenticate_archive(source, root)
    stored = {file.file_id: file for file in source.files}
    result = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        paths = {}
        for info in archive.infolist():
            if not info.is_dir():
                path = (
                    info.filename.split("/", 1)[1]
                    if source.policy.strip_single_root
                    else info.filename
                )
                paths[path] = info
        for selected in source.request.files:
            file = stored[selected.file_id]
            with archive.open(paths[file.path]) as stream:
                data = stream.read(selected.size_bytes + 1)
            if (
                len(data) != selected.size_bytes
                or hashlib.sha256(data).hexdigest() != selected.sha256
            ):
                raise SourceIntegrityError("source_integrity_error")
            SourceIndex(data)
            result[selected.file_id] = data
    return result
