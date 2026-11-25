"""A workspace directory on the host that a hostile process can write to, accessed safely.

The sandboxed process shares this directory and can create anything in it: symlinks to host paths,
FIFOs, devices, directories where a file was, a symlink swapped in while a call runs. Every
operation therefore walks the path one component at a time with ``O_NOFOLLOW`` and directory file
descriptors, so a link is never followed and a swap cannot redirect an open call. Blocking calls
only; the adapter runs them in a thread.
"""

import errno
import hashlib
import os
import secrets
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from app.ports.sandbox import (
    MAX_FILE_BYTES,
    MAX_WORKSPACE_FILES,
    NotATextFile,
    PathRejected,
    SandboxLimit,
)

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_TMP_PREFIX = ".cs-"
_TMP_SUFFIX = ".tmp"
_LINK_ERRORS = {errno.ELOOP, errno.ENOTDIR}


def _is_temp(name: str) -> bool:
    return name.startswith(_TMP_PREFIX) and name.endswith(_TMP_SUFFIX)


@dataclass(frozen=True)
class FileFact:
    size: int
    sha256: str
    mode: int = 0o644


class HostTree:
    """Files under one root, addressed by canonical relative paths."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o755)
        self._hashes: dict[str, tuple[int, int, int, str]] = {}

    # --- walking one component at a time ------------------------------------------------------

    def _open_root(self) -> int:
        return os.open(self.root, _DIR)

    def _open_dir(self, parts: list[str], *, create: bool = False) -> int:
        fd = self._open_root()
        try:
            for part in parts:
                try:
                    child = os.open(part, _DIR, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, 0o755, dir_fd=fd)
                    child = os.open(part, _DIR, dir_fd=fd)
                except OSError as exc:
                    if exc.errno in _LINK_ERRORS:
                        raise PathRejected("path crosses a link or a file") from None
                    raise
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _split(rel: str) -> tuple[list[str], str]:
        *parents, name = rel.split("/")
        return parents, name

    # --- inspection ---------------------------------------------------------------------------

    def kind(self, rel: str) -> str | None:
        """``file``, ``dir``, ``other`` (link, FIFO, device) or ``None`` when absent."""
        parents, name = self._split(rel)
        try:
            dfd = self._open_dir(parents)
        except (FileNotFoundError, PathRejected):
            return None
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        finally:
            os.close(dfd)
        if stat.S_ISREG(st.st_mode):
            return "file"
        return "dir" if stat.S_ISDIR(st.st_mode) else "other"

    def dir_exists(self, rel: str) -> bool:
        if rel == ".":
            return True
        parts = rel.split("/")
        try:
            os.close(self._open_dir(parts))
        except (FileNotFoundError, PathRejected):
            return False
        return True

    def has_children(self, rel: str) -> bool:
        return self.kind(rel) == "dir"

    def read(self, rel: str, max_bytes: int = MAX_FILE_BYTES) -> bytes | None:
        """A regular file's bytes, or ``None`` when absent. Links and special files are refused."""
        parents, name = self._split(rel)
        try:
            dfd = self._open_dir(parents)
        except (FileNotFoundError, PathRejected):
            return None
        try:
            try:
                fd = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd
                )
            except FileNotFoundError:
                return None
            except OSError as exc:
                if exc.errno in _LINK_ERRORS | {errno.ENXIO, errno.EISDIR}:
                    raise NotATextFile("not a regular file") from None
                raise
        finally:
            os.close(dfd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise NotATextFile("not a regular file")
            if st.st_size > max_bytes:
                raise SandboxLimit("file exceeds the size limit")
            chunks = []
            remaining = st.st_size
            while remaining > 0:
                chunk = os.read(fd, min(remaining, 1 << 20))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    # --- enumeration --------------------------------------------------------------------------

    def _walk(
        self, fd: int, prefix: str, counter: list[int]
    ) -> Iterator[tuple[str, os.stat_result]]:
        with os.scandir(fd) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                name = entry.name
                if _is_temp(name) or name == ".git":
                    continue
                rel = f"{prefix}{name}"
                if entry.is_dir(follow_symlinks=False):
                    try:
                        child = os.open(name, _DIR, dir_fd=fd)
                    except OSError:
                        continue
                    try:
                        yield from self._walk(child, rel + "/", counter)
                    finally:
                        os.close(child)
                elif entry.is_file(follow_symlinks=False):
                    counter[0] += 1
                    if counter[0] > MAX_WORKSPACE_FILES:
                        raise SandboxLimit("workspace has too many files")
                    yield rel, entry.stat(follow_symlinks=False)

    def files(self) -> Iterator[tuple[str, os.stat_result]]:
        """Regular files sorted by path; links, special files, .git and temp files are skipped."""
        fd = self._open_root()
        try:
            yield from self._walk(fd, "", [0])
        finally:
            os.close(fd)

    def total_bytes(self) -> int:
        return sum(st.st_size for _, st in self.files())

    def facts(self) -> dict[str, FileFact]:
        """Size and SHA-256 of every file. Unchanged files (size, time, inode) reuse their hash."""
        result: dict[str, FileFact] = {}
        seen: dict[str, tuple[int, int, int, str]] = {}
        for rel, st in self.files():
            key = (st.st_size, st.st_mtime_ns, st.st_ino)
            cached = self._hashes.get(rel)
            digest = cached[3] if cached and cached[:3] == key else self._hash(rel)
            if digest is None:
                continue  # replaced or removed while walking
            seen[rel] = (*key, digest)
            result[rel] = FileFact(st.st_size, digest, stat.S_IMODE(st.st_mode))
        self._hashes = seen
        return result

    def _hash(self, rel: str) -> str | None:
        parents, name = self._split(rel)
        try:
            dfd = self._open_dir(parents)
        except (FileNotFoundError, PathRejected):
            return None
        try:
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd
            )
        except OSError:
            return None
        finally:
            os.close(dfd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            h = hashlib.sha256()
            while chunk := os.read(fd, 1 << 20):
                h.update(chunk)
            return h.hexdigest()
        finally:
            os.close(fd)

    # --- changes ------------------------------------------------------------------------------

    def write(self, rel: str, data: bytes) -> None:
        """Replace or create a file atomically. An existing file keeps its permission bits."""
        parents, name = self._split(rel)
        dfd = self._open_dir(parents, create=True)
        try:
            mode = 0o644
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISDIR(st.st_mode):
                    raise PathRejected("path is a directory")
                if stat.S_ISREG(st.st_mode):
                    mode = st.st_mode & 0o777
            tmp = f"{_TMP_PREFIX}{secrets.token_hex(8)}{_TMP_SUFFIX}"
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=dfd,
            )
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
                os.fchmod(fd, mode)
            except BaseException:
                os.close(fd)
                os.unlink(tmp, dir_fd=dfd)
                raise
            os.close(fd)
            try:
                os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            except BaseException:
                os.unlink(tmp, dir_fd=dfd)
                raise
        finally:
            os.close(dfd)

    def export(self, rel: str, dest: Path) -> None:
        """Copy a file to ``dest`` (a host-controlled path), streaming, never following links."""
        parents, name = self._split(rel)
        dfd = self._open_dir(parents)
        try:
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd
            )
        finally:
            os.close(dfd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise NotATextFile("not a regular file")
            with dest.open("wb") as out:
                while chunk := os.read(fd, 1 << 20):
                    out.write(chunk)
        finally:
            os.close(fd)

    def import_file(self, rel: str, src: Path, mode: int = 0o644) -> None:
        """Create or replace a file from ``src`` (a path the host controls), atomically."""
        parents, name = self._split(rel)
        dfd = self._open_dir(parents, create=True)
        try:
            try:
                if stat.S_ISDIR(os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mode):
                    raise PathRejected("path is a directory")
            except FileNotFoundError:
                pass
            tmp = f"{_TMP_PREFIX}{secrets.token_hex(8)}{_TMP_SUFFIX}"
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=dfd,
            )
            try:
                with src.open("rb") as source:
                    while chunk := source.read(1 << 20):
                        view = memoryview(chunk)
                        while view:
                            view = view[os.write(fd, view) :]
                os.fchmod(fd, mode)
            except BaseException:
                os.close(fd)
                os.unlink(tmp, dir_fd=dfd)
                raise
            os.close(fd)
            try:
                os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            except BaseException:
                os.unlink(tmp, dir_fd=dfd)
                raise
        finally:
            os.close(dfd)

    def delete(self, rel: str) -> None:
        parents, name = self._split(rel)
        dfd = self._open_dir(parents)
        try:
            os.unlink(name, dir_fd=dfd)
        finally:
            os.close(dfd)

    def move(self, src: str, dst: str) -> None:
        sp, sn = self._split(src)
        dp, dn = self._split(dst)
        sfd = self._open_dir(sp)
        try:
            dfd = self._open_dir(dp, create=True)
            try:
                os.rename(sn, dn, src_dir_fd=sfd, dst_dir_fd=dfd)
            finally:
                os.close(dfd)
        finally:
            os.close(sfd)

    def clear(self) -> None:
        """Remove everything under the root without following links."""
        fd = self._open_root()
        try:
            self._remove_children(fd)
        finally:
            os.close(fd)
        self._hashes = {}

    def _remove_children(self, fd: int) -> None:
        with os.scandir(fd) as entries:
            items = list(entries)
        for entry in items:
            if entry.is_dir(follow_symlinks=False):
                try:
                    child = os.open(entry.name, _DIR, dir_fd=fd)
                except OSError:
                    continue
                try:
                    self._remove_children(child)
                finally:
                    os.close(child)
                os.rmdir(entry.name, dir_fd=fd)
            else:
                os.unlink(entry.name, dir_fd=fd)
