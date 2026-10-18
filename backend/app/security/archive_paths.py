import re
import unicodedata


class UnsafeArchivePath(ValueError):  # noqa: N818 -- public path rejection contract
    pass


_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:[.]|$)", re.I)


def validate_archive_path(path: str, *, max_bytes: int = 1024, max_depth: int = 32) -> str:
    directory = path.endswith("/")
    value = path[:-1] if directory else path
    parts = value.split("/")
    if (
        not value
        or len(value.encode()) > max_bytes
        or len(parts) > max_depth
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or "\\" in value
        or any(
            p in {"", ".", ".."} or ":" in p or p.endswith((".", " ")) or _RESERVED.match(p)
            for p in parts
        )
    ):
        raise UnsafeArchivePath("unsafe_archive_path")
    return value


def collision_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()
