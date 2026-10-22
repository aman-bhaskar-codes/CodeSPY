"""Bounded lockfile parsing without installation, resolution or network access."""

import json
import re
import tomllib
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from packaging.version import InvalidVersion, Version

from app.domain.errors import PersistenceError


class LockfileError(PersistenceError):
    def __init__(self) -> None:
        super().__init__("lockfile_unsupported_or_invalid")


@dataclass(frozen=True, slots=True)
class LockedPackage:
    ecosystem: Literal["npm", "PyPI"]
    name: str
    version: str
    location: str
    markers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LockInventory:
    packages: tuple[LockedPackage, ...]
    unresolved: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.unresolved


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LockfileError()
        result[key] = value
    return result


def _npm_name(path: str) -> str:
    parts = path.split("node_modules/")
    if parts[0] or len(parts) < 2:
        raise LockfileError()
    for index, part in enumerate(parts[1:]):
        name = part.removesuffix("/") if index < len(parts) - 2 else part
        if not re.fullmatch(r"(?:@[a-z0-9._-]+/)?[a-z0-9._-]+", name) or ".." in name:
            raise LockfileError()
    return parts[-1]


def _registry_tarball(value: Any, name: str) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "registry.npmjs.org"
        and not parsed.query
        and not parsed.fragment
        and unquote(parsed.path).startswith("/" + name + "/-/")
        and parsed.path.endswith(".tgz")
    )


def _npm_version(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 128:
        return False
    match = re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
        r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
        value,
    )
    if match is None:
        return False
    return all(
        not part.isdigit() or len(part) == 1 or part[0] != "0"
        for part in (match.group("pre") or "").split(".")
    )


def parse_package_lock(raw: bytes) -> LockInventory:
    """Support package-lock v3 exact public npm tarballs; preserve install locations."""
    try:
        if len(raw) > 262144:
            raise LockfileError()
        document = json.loads(raw, object_pairs_hook=_unique)
        if type(document["lockfileVersion"]) is not int or document["lockfileVersion"] != 3:
            raise LockfileError()
        entries = document["packages"]
        if not isinstance(entries, dict) or len(entries) > 5000:
            raise LockfileError()
        packages, unresolved = [], []
        for path, entry in sorted(entries.items()):
            if path == "":
                continue
            name = _npm_name(path)
            if not isinstance(entry, dict):
                raise LockfileError()
            identity = entry.get("name", name)
            version = entry.get("version", "")
            if (
                not isinstance(identity, str)
                or not re.fullmatch(r"(?:@[a-z0-9._-]+/)?[a-z0-9._-]+", identity)
                or not _npm_version(version)
                or len(identity) > 214
                or len(version) > 128
                or entry.get("link", False)
                or not _registry_tarball(entry.get("resolved"), identity)
            ):
                unresolved.append(path)
                continue
            packages.append(LockedPackage("npm", identity, version, path))
        return LockInventory(tuple(packages), tuple(unresolved))
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise LockfileError() from None


def parse_uv_lock(raw: bytes) -> LockInventory:
    """Support uv lock v1 revision3 public PyPI identities without evaluating markers."""
    try:
        if len(raw) > 262144:
            raise LockfileError()
        document = tomllib.loads(raw.decode("utf-8"))
        if (
            type(document.get("version")) is not int
            or document["version"] != 1
            or type(document.get("revision")) is not int
            or document["revision"] != 3
        ):
            raise LockfileError()
        entries = document["package"]
        if not isinstance(entries, list) or len(entries) > 5000:
            raise LockfileError()
        packages, unresolved = [], []
        for index, entry in enumerate(entries):
            name, version = entry["name"], entry.get("version", "")
            if not isinstance(name, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", name
            ):
                raise LockfileError()
            name = re.sub(r"[-_.]+", "-", name).lower()
            source = entry.get("source", {})
            if source == {"virtual": "."} or source == {"editable": "."}:
                continue
            if source != {"registry": "https://pypi.org/simple"}:
                unresolved.append(f"package[{index}]")
                continue
            if not isinstance(version, str) or len(version) > 128:
                unresolved.append(f"package[{index}]")
                continue
            try:
                Version(version)
            except InvalidVersion:
                unresolved.append(f"package[{index}]")
                continue
            markers = entry.get("resolution-markers", [])
            if (
                not isinstance(markers, list)
                or len(markers) > 32
                or any(not isinstance(m, str) or len(m) > 512 for m in markers)
            ):
                raise LockfileError()
            packages.append(
                LockedPackage("PyPI", name, version, f"package[{index}]", tuple(markers))
            )
        return LockInventory(tuple(packages), tuple(unresolved))
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise LockfileError() from None
