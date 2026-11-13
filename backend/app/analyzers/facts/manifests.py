"""Package manifest facts: names only, read with the standard library, never executed.

Supported: ``go.mod``, ``Cargo.toml``, ``package.json``, ``pyproject.toml`` and ``Package.swift``.
Versions, URLs, scripts and anything credential-like are dropped; a dependency is its name.
``Package.swift`` is code, so only literal target and package names are scanned for, by pattern.
"""

import json
import re
import tomllib
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from app.contracts.ast_facts import FileFactsV1, ManifestFactV1
from app.contracts.structure import AnalysisBudget, SourceUnitV1

MANIFEST_NAMES: dict[str, str] = {
    "go.mod": "go",
    "Cargo.toml": "cargo",
    "package.json": "npm",
    "pyproject.toml": "python",
    "Package.swift": "swift",
}
MAX_MANIFEST_BYTES = 256 * 1024
MAX_DEPS, MAX_MEMBERS, MAX_TARGETS = 512, 64, 64


def is_manifest(path: str) -> bool:
    return PurePosixPath(path).name in MANIFEST_NAMES


def _table(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _name(value: Any, limit: int = 256) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > limit or "://" in value or any(ord(c) < 32 for c in value):
        return None
    return str(value)


class _Collector:
    def __init__(self) -> None:
        self.deps: dict[str, str] = {}
        self.members: list[str] = []
        self.targets: list[str] = []
        self.truncated = False

    def dep(self, name: Any, scope: str = "runtime") -> None:
        clean = _name(name)
        if clean is None:
            return
        if clean not in self.deps and len(self.deps) >= MAX_DEPS:
            self.truncated = True
            return
        if self.deps.get(clean) != "runtime":  # runtime wins over dev
            self.deps[clean] = scope

    def keys(self, table: Any, scope: str = "runtime") -> None:
        if isinstance(table, dict):
            for key in sorted(table):
                self.dep(key, scope)

    def member(self, value: Any) -> None:
        clean = _name(value, 512)
        if clean is None:
            return
        if len(self.members) >= MAX_MEMBERS:
            self.truncated = True
        elif clean not in self.members:
            self.members.append(clean)

    def target(self, value: Any) -> None:
        clean = _name(value)
        if clean is None:
            return
        if len(self.targets) >= MAX_TARGETS:
            self.truncated = True
        elif clean not in self.targets:
            self.targets.append(clean)

    def fact(self, ecosystem: str, name: str | None) -> ManifestFactV1:
        return ManifestFactV1.model_validate_json(
            json.dumps(
                {
                    "ecosystem": ecosystem,
                    "name": _name(name),
                    "dependencies": [{"name": n, "scope": s} for n, s in sorted(self.deps.items())],
                    "members": self.members,
                    "targets": self.targets,
                    "truncated": self.truncated,
                }
            )
        )


def parse_go_mod(text: str) -> ManifestFactV1:
    out = _Collector()
    module: str | None = None
    block: str | None = None
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if block:
            if line == ")":
                block = None
            elif block == "require":
                out.dep(line.split()[0])
            continue
        head, _, rest = line.partition(" ")
        rest = rest.strip()
        if head == "module":
            module = rest.strip('"`')
        elif head == "require":
            if rest == "(":
                block = "require"
            elif rest:
                out.dep(rest.split()[0])
        elif head in {"replace", "exclude", "retract", "tool", "godebug"} and rest == "(":
            block = head
    return out.fact("go", module)


def parse_cargo(text: str) -> ManifestFactV1:
    data = tomllib.loads(text)
    out = _Collector()
    package = _table(data.get("package"))
    workspace = _table(data.get("workspace"))

    def table(values: Any, scope: str) -> None:
        if not isinstance(values, dict):
            return
        for key in sorted(values):
            spec = values[key]
            real = spec.get("package") if isinstance(spec, dict) else None
            out.dep(real or key, scope)

    for section, scope in (
        ("dependencies", "runtime"),
        ("build-dependencies", "runtime"),
        ("dev-dependencies", "dev"),
    ):
        table(data.get(section), scope)
        table(workspace.get(section), scope)
        targets = data.get("target")
        if isinstance(targets, dict):
            for cfg in sorted(targets):
                if isinstance(targets[cfg], dict):
                    table(targets[cfg].get(section), scope)
    members = workspace.get("members")
    for member in members if isinstance(members, list) else []:
        out.member(member)
    for key in ("lib", "bin"):
        entries = data.get(key)
        for entry in entries if isinstance(entries, list) else [entries]:
            if isinstance(entry, dict):
                out.target(entry.get("name"))
    return out.fact("cargo", package.get("name"))


def parse_package_json(text: str) -> ManifestFactV1:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("package.json is not an object")
    out = _Collector()
    for section, scope in (
        ("dependencies", "runtime"),
        ("optionalDependencies", "runtime"),
        ("peerDependencies", "runtime"),
        ("devDependencies", "dev"),
    ):
        out.keys(data.get(section), scope)
    workspaces = data.get("workspaces")
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages")
    for member in workspaces if isinstance(workspaces, list) else []:
        out.member(member)
    return out.fact("npm", data.get("name"))


_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _requirement(value: Any) -> str | None:
    match = _REQ_NAME.match(value) if isinstance(value, str) else None
    return match.group(1) if match else None


def parse_pyproject(text: str) -> ManifestFactV1:
    data = tomllib.loads(text)
    out = _Collector()
    project = _table(data.get("project"))
    tool = _table(data.get("tool"))
    poetry = _table(tool.get("poetry"))
    name = project.get("name") or poetry.get("name")
    for requirement in project.get("dependencies") or []:
        out.dep(_requirement(requirement))
    for table in (
        _table(project.get("optional-dependencies")),
        _table(data.get("dependency-groups")),
    ):
        for group in sorted(table):
            for requirement in table[group] if isinstance(table[group], list) else []:
                out.dep(_requirement(requirement), "dev")
    for key in sorted(_table(poetry.get("dependencies"))):
        if key.lower() != "python":
            out.dep(key)
    poetry_groups = _table(poetry.get("group"))
    for group in sorted(poetry_groups):
        out.keys(_table(poetry_groups[group]).get("dependencies"), "dev")
    return out.fact("python", name)


_SWIFT_PACKAGE = re.compile(r"Package\s*\(\s*name\s*:\s*\"([^\"\n]{1,256})\"")
_SWIFT_TARGET = re.compile(
    r"\.(?:target|executableTarget|testTarget|macro|plugin|systemLibrary|binaryTarget)"
    r"\s*\(\s*name\s*:\s*\"([^\"\n]{1,256})\""
)
_SWIFT_PACKAGE_DEP = re.compile(r"\.package\s*\([^)]*?url\s*:\s*\"([^\"\n]{1,512})\"")


def parse_package_swift(text: str) -> ManifestFactV1:
    out = _Collector()
    for match in _SWIFT_TARGET.finditer(text):
        out.target(match.group(1))
    for match in _SWIFT_PACKAGE_DEP.finditer(text):
        # Only the repository's own name is kept; the host, owner and any credentials are dropped.
        last = match.group(1).rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        out.dep(last)
    package = _SWIFT_PACKAGE.search(text)
    return out.fact("swift", package.group(1) if package else None)


PARSERS: dict[str, Callable[[str], ManifestFactV1]] = {
    "go": parse_go_mod,
    "cargo": parse_cargo,
    "npm": parse_package_json,
    "python": parse_pyproject,
    "swift": parse_package_swift,
}


def parse_manifest(path: str, data: bytes) -> ManifestFactV1 | None:
    """The manifest's facts, or ``None`` when it is oversized, not text, or does not parse."""
    ecosystem = MANIFEST_NAMES.get(PurePosixPath(path).name)
    if ecosystem is None or len(data) > MAX_MANIFEST_BYTES:
        return None
    try:
        return PARSERS[ecosystem](data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None  # tomllib.TOMLDecodeError and json errors are ValueErrors


def extract(unit: SourceUnitV1, budget: AnalysisBudget) -> FileFactsV1:
    budget.tick_nodes(1)
    manifest = parse_manifest(unit.path, unit.data)
    return FileFactsV1(
        file_id=unit.file_id,
        path=unit.path,
        sha256=unit.sha256,
        language="manifest",
        grammar=f"manifest-{MANIFEST_NAMES.get(PurePosixPath(unit.path).name, 'unknown')}-v1",
        status="parsed" if manifest is not None else "partial",
        error_nodes=0 if manifest is not None else 1,
        manifest=manifest,
        diagnostics=(),
    )


__all__ = ["MANIFEST_NAMES", "extract", "is_manifest", "parse_manifest"]
