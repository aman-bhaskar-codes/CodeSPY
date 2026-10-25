"""Hostile archive fixtures for the isolated source-reading boundary."""

import hashlib
import io
import stat
import warnings
import zipfile

import pytest
from app.source_read.extract import SourceReadRejected, read_admitted_entry


def archive(entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as source:
            for name, value in entries:
                source.writestr(name, value)
    return buf.getvalue()


def read(
    raw: bytes, *, path: str = "src/main.py", content: bytes = b"print(1)", strip_root: bool = False
) -> bytes:
    return read_admitted_entry(
        raw,
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        strip_root=strip_root,
    )


def test_exact_entry_and_github_root() -> None:
    assert read(archive([("src/main.py", b"print(1)")])) == b"print(1)"
    assert read(archive([("repo-sha/src/main.py", b"print(1)")]), strip_root=True) == b"print(1)"


@pytest.mark.parametrize("name", ["../main.py", "/main.py", "a\\main.py"])
def test_rejects_unsafe_paths(name: str) -> None:
    with pytest.raises(SourceReadRejected):
        read(archive([(name, b"print(1)")]), path=name)


def test_rejects_duplicate_members_even_with_identical_bytes() -> None:
    with pytest.raises(SourceReadRejected):
        read(archive([("src/main.py", b"print(1)"), ("src/main.py", b"print(1)")]))


def test_rejects_symlink() -> None:
    link = zipfile.ZipInfo("src/main.py")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(SourceReadRejected):
        read(archive([(link, b"print(1)")]))


@pytest.mark.parametrize(
    "raw",
    [b"not a zip", archive([("other.py", b"print(1)")]), archive([("src/main.py", b"print(2)")])],
)
def test_corrupt_missing_or_changed_source_is_rejected(raw: bytes) -> None:
    with pytest.raises(SourceReadRejected):
        read(raw)


def test_rejects_declared_size_mismatch_and_file_limit() -> None:
    raw = archive([("src/main.py", b"print(1)")])
    with pytest.raises(SourceReadRejected):
        read(raw, content=b"x")
    with pytest.raises(SourceReadRejected):
        read_admitted_entry(
            raw, path="src/main.py", sha256="0" * 64, size_bytes=2_097_153, strip_root=False
        )


def test_root_stripping_rejects_multiple_roots() -> None:
    with pytest.raises(SourceReadRejected):
        read(archive([("one/src/main.py", b"print(1)"), ("two/other.py", b"")]), strip_root=True)
