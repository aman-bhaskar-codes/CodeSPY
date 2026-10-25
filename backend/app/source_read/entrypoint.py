"""Fixed offline entrypoint; return verified bytes without emitting source diagnostics."""

import hashlib
import os
import stat
import sys
from pathlib import Path

from app.ports.source_reader import AdmittedSourceReadV1
from app.source_read.extract import read_admitted_entry


def read_fixed(root: Path, filename: str, maximum: int) -> bytes:
    fd = os.open(root / filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("source_read_rejected")
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise ValueError("source_read_rejected")
    return value


def main() -> int:
    try:
        root = Path("/input")
        request = AdmittedSourceReadV1.model_validate_json(read_fixed(root, "request.json", 16384))
        raw = read_fixed(root, "archive.zip", 104_857_600)
        if (
            len(raw) != request.archive.size_bytes
            or hashlib.sha256(raw).hexdigest() != request.archive.sha256
        ):
            return 65
        content = read_admitted_entry(
            raw,
            path=request.path,
            sha256=request.sha256,
            size_bytes=request.size_bytes,
            strip_root=request.strip_root,
        )
        sys.stdout.buffer.write(content)
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        return 65


if __name__ == "__main__":
    raise SystemExit(main())
