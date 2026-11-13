"""Deterministic dependency graph from published per-file facts."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.analyzers.graph.index import EXTERNAL, FILE, PACKAGE, PathIndex, parent
from app.analyzers.graph.resolvers import RESOLVERS, Ctx, Outcome
from app.contracts.architecture_report import (
    MAX_EDGES,
    MAX_NODES,
    DependencyGraphV1,
    EvidenceRefV1,
    GraphEdgeV1,
    GraphNodeV1,
    ReportTooLarge,
    Resolution,
)
from app.contracts.ast_facts import FileFactsV1

ECOSYSTEM = {"javascript": "js", "jsx": "js", "typescript": "js", "tsx": "js", "cpp": "c"}


@dataclass(frozen=True)
class FileEntry:
    file_id: UUID
    path: str
    language: str
    facts: FileFactsV1


def _label(node_id: str) -> str:
    return node_id.split(":", 1)[1] if ":" in node_id else node_id


def declared_identity(manifests: Iterable[FileEntry]) -> dict[str, Any]:
    """Package identity (Go modules, npm workspace packages, Cargo crates) from manifest facts."""
    go_modules: list[tuple[str, str]] = []
    npm: dict[str, str] = {}
    cargo: dict[str, str] = {}
    for entry in sorted(manifests, key=lambda m: m.path):
        fact = entry.facts.manifest
        if fact is None or not fact.name:
            continue
        directory = parent(entry.path)
        if fact.ecosystem == "go":
            go_modules.append((fact.name, directory))
        elif fact.ecosystem == "npm":
            npm.setdefault(fact.name, directory)
        elif fact.ecosystem == "cargo":
            cargo.setdefault(fact.name.replace("-", "_"), directory)
    return {"go_modules": go_modules, "npm_packages": npm, "cargo_crates": cargo}


def build_graph(
    files: Sequence[FileEntry],
    inventory: Iterable[str] = (),
    manifests: Sequence[FileEntry] = (),
) -> DependencyGraphV1:
    """Resolve every import fact; the result depends only on the facts and the inventory."""
    ordered = sorted(files, key=lambda f: f.path)
    index = PathIndex.build((f.path for f in ordered), inventory)
    states: dict[str, dict[str, object]] = {}
    declared = declared_identity(manifests)

    def resolve_all(emit: bool) -> list[tuple[FileEntry, int, Outcome, str]]:
        found: list[tuple[FileEntry, int, Outcome, str]] = []
        for entry in ordered:
            resolver = RESOLVERS.get(entry.language)
            if resolver is None:
                continue
            ctx = Ctx(entry.path, index, states.setdefault(entry.language, {}), declared)
            for position, imp in enumerate(entry.facts.imports):
                for outcome in resolver(imp, ctx):
                    if emit:
                        found.append((entry, position, outcome, imp.module))
        return found

    resolve_all(emit=False)  # first pass lets a language learn context (e.g. Go module prefixes)
    resolved = resolve_all(emit=True)

    nodes: dict[str, GraphNodeV1] = {
        FILE + f.path: GraphNodeV1(
            id=FILE + f.path, kind="file", label=f.path, language=f.language, file_id=f.file_id
        )
        for f in ordered
    }

    def ensure(node_id: str, language: str) -> str:
        if node_id in nodes:
            return node_id
        is_package = node_id.startswith(PACKAGE)
        nodes[node_id] = GraphNodeV1(
            id=node_id,
            kind="package" if is_package else "external",
            label=_label(node_id),
            language=None if is_package else language,
        )
        return node_id

    edges: list[GraphEdgeV1] = []
    for entry, position, outcome, module in resolved:
        imp = entry.facts.imports[position]
        source = FILE + entry.path
        evidence = EvidenceRefV1(
            file_id=entry.file_id, path=entry.path, fact="import", range=imp.range
        )
        module_text = module[:512]

        def edge(
            target: str | None,
            resolution: Resolution,
            candidates: tuple[str, ...] = (),
            *,
            _source: str = source,
            _kind: str = imp.kind,
            _module: str = module_text,
            _evidence: EvidenceRefV1 = evidence,
        ) -> GraphEdgeV1:
            return GraphEdgeV1(
                source=_source,
                target=target,
                candidates=candidates,
                module=_module,
                import_kind=_kind,
                resolution=resolution,
                evidence=_evidence,
            )

        if outcome.resolution == "resolved":
            for target in outcome.targets:
                if target == source:
                    continue
                ensure(target, entry.language)
                edges.append(edge(target, "resolved"))
        elif outcome.resolution == "ambiguous":
            candidates = tuple(ensure(t, entry.language) for t in outcome.targets)
            edges.append(edge(None, "ambiguous", candidates))
        elif outcome.resolution == "external":
            family = ECOSYSTEM.get(entry.language, entry.language)
            target = ensure(f"{EXTERNAL}{family}:{(outcome.external or '')[:256]}", family)
            edges.append(edge(target, "external"))
        else:
            edges.append(edge(None, "unresolved"))
        if len(edges) > MAX_EDGES or len(nodes) > MAX_NODES:
            raise ReportTooLarge("dependency graph exceeds its size bound")

    edges.sort(
        key=lambda e: (
            e.source,
            e.evidence.range.start_byte if e.evidence.range else 0,
            e.module,
            e.target or "",
            e.resolution,
        )
    )
    return DependencyGraphV1(
        nodes=tuple(sorted(nodes.values(), key=lambda n: n.id)), edges=tuple(edges)
    )
