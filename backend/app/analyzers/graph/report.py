"""Assemble the evidence-first architecture report from published per-file facts.

Pure and deterministic: the same facts, inventory and coverage always give the same report. Every
statement is typed (observation, derivation, inference, unknown). Counts and rankings are
derivations; "this project uses framework X" and "this looks like an entry point" are inferences;
what the facts cannot settle (unresolved imports, partial files, truncated coverage, fixed
limitations) is stated as unknown. Statements carry evidence (file and range), never source text.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from uuid import UUID

from app.analyzers.graph.analytics import Analytics, analyze
from app.analyzers.graph.build import FileEntry, build_graph
from app.contracts.architecture_report import (
    ArchitectureReportV1,
    CoverageV1,
    DependencyGraphV1,
    EvidenceRefV1,
    Scalar,
    SectionV1,
    StatementV1,
    TableV1,
    inputs_digest,
)
from app.contracts.ast_facts import FileFactsV1
from app.domain.source_ranges import SourceRangeV1

MAX_TABLE_ROWS = 200
MAX_EVIDENCE = 8

# External import roots that indicate a framework. Detection is an inference, never an observation.
FRAMEWORKS: dict[tuple[str, str], str] = {
    ("python", "flask"): "Flask",
    ("python", "django"): "Django",
    ("python", "fastapi"): "FastAPI",
    ("python", "sqlalchemy"): "SQLAlchemy",
    ("python", "pydantic"): "Pydantic",
    ("python", "celery"): "Celery",
    ("js", "express"): "Express",
    ("js", "react"): "React",
    ("js", "next"): "Next.js",
    ("js", "vue"): "Vue",
    ("js", "koa"): "Koa",
    ("js", "@nestjs/core"): "NestJS",
    ("go", "github.com/gin-gonic/gin"): "Gin",
    ("go", "github.com/labstack/echo"): "Echo",
    ("java", "org.springframework"): "Spring",
    ("java", "jakarta.persistence"): "JPA",
    ("rust", "tokio"): "Tokio",
    ("rust", "axum"): "Axum",
    ("rust", "serde"): "Serde",
    ("ruby", "sinatra"): "Sinatra",
    ("ruby", "rails"): "Rails",
    ("php", "Illuminate"): "Laravel",
    ("php", "Slim"): "Slim",
    ("swift", "Alamofire"): "Alamofire",
    ("swift", "SwiftUI"): "SwiftUI",
}


def _clip(value: str | None, limit: int = 256) -> str:
    return (value or "")[:limit]


def _ref(entry: FileEntry, fact: str, range_: SourceRangeV1 | None) -> EvidenceRefV1:
    return EvidenceRefV1(
        file_id=entry.file_id,
        path=entry.path,
        fact=fact,  # type: ignore[arg-type]
        range=range_,
    )


def _statement(
    kind: str,
    code: str,
    params: dict[str, Scalar] | None = None,
    evidence: Sequence[EvidenceRefV1] = (),
) -> StatementV1:
    return StatementV1(
        kind=kind,  # type: ignore[arg-type]
        code=code,
        params=params or {},
        evidence=tuple(evidence[:MAX_EVIDENCE]),
        evidence_total=len(evidence),
    )


def _table(name: str, columns: tuple[str, ...], rows: Sequence[tuple[Scalar, ...]]) -> TableV1:
    return TableV1(
        name=name,
        columns=columns,
        rows=tuple(rows[:MAX_TABLE_ROWS]),
        rows_total=len(rows),
    )


def _overview(
    files: Sequence[FileEntry], graph: DependencyGraphV1, stack: list[StatementV1]
) -> SectionV1:
    by_language: Counter[str] = Counter(f.language for f in files)
    symbols: Counter[str] = Counter()
    for f in files:
        symbols[f.language] += len(f.facts.symbols)
    rows = [
        (lang, n, symbols[lang])
        for lang, n in sorted(by_language.items(), key=lambda x: (-x[1], x[0]))
    ]
    statements = [
        _statement("derivation", "overview.files", {"analyzed": len(files)}),
        _statement("derivation", "overview.symbols", {"total": sum(symbols.values())}),
    ]
    if rows:
        statements.append(
            _statement(
                "derivation",
                "overview.primary_language",
                {"language": rows[0][0], "files": rows[0][1]},
            )
        )
    statements.extend(stack)
    return SectionV1(
        name="overview",
        statements=tuple(statements),
        tables=(_table("languages", ("language", "files", "symbols"), rows),),
    )


def _frameworks(files: Sequence[FileEntry], graph: DependencyGraphV1) -> list[StatementV1]:
    entries = {f.path: f for f in files}
    evidence: dict[str, list[EvidenceRefV1]] = defaultdict(list)
    for edge in graph.edges:
        if edge.resolution != "external" or edge.target is None:
            continue
        family, _, root = edge.target[len("ext:") :].partition(":")
        for (language, prefix), name in FRAMEWORKS.items():
            if family == language and (root == prefix or root.startswith(prefix + "/")):
                evidence[name].append(edge.evidence)
    out = []
    for name in sorted(evidence):
        users = {e.path for e in evidence[name]}
        out.append(
            _statement(
                "inference",
                "stack.framework",
                {"framework": name, "files": len(users)},
                evidence[name],
            )
        )
    del entries
    return out


def _components(a: Analytics) -> SectionV1:
    rows = [
        (
            c.name,
            c.files,
            ", ".join(f"{k}:{v}" for k, v in c.languages[:3]),
            c.inbound,
            c.outbound,
            -1 if c.layer is None else c.layer,
        )
        for c in a.components
    ]
    statements = [
        _statement("derivation", "components.count", {"count": len(a.components)}),
        _statement("derivation", "components.layers", {"layers": len(a.layers)}),
        _statement("inference", "components.grouping_heuristic", {"max_components": 40}),
    ]
    for cycle in a.component_cycles[:20]:
        statements.append(
            _statement(
                "derivation",
                "components.cycle",
                {"size": len(cycle), "members": ", ".join(cycle)[:500]},
            )
        )
    return SectionV1(
        name="components",
        statements=tuple(statements),
        tables=(
            _table(
                "components",
                ("component", "files", "languages", "inbound", "outbound", "layer"),
                rows,
            ),
        ),
    )


def _dependencies(files: Sequence[FileEntry], graph: DependencyGraphV1, a: Analytics) -> SectionV1:
    counts = graph.counts()
    statements = [
        _statement("derivation", "dependencies.counts", dict(counts)),
        _statement("derivation", "dependencies.external", {"packages": len(a.external)}),
    ]
    for cycle in a.file_cycles[:20]:
        statements.append(
            _statement(
                "derivation",
                "dependencies.cycle",
                {
                    "size": len(cycle),
                    "members": ", ".join(m.removeprefix("file:") for m in cycle[:5])[:500],
                },
            )
        )
    for hub in a.hubs_in[:1]:
        statements.append(
            _statement(
                "derivation", "dependencies.top_imported", {"node": hub.node, "count": hub.count}
            )
        )
    unresolved = [e for e in graph.edges if e.resolution == "unresolved"]
    ambiguous = [e for e in graph.edges if e.resolution == "ambiguous"]
    internal = counts["resolved"] + counts["ambiguous"] + counts["unresolved"]
    if unresolved:
        statements.append(
            _statement(
                "unknown",
                "dependencies.unresolved",
                {
                    "edges": len(unresolved),
                    "permille_of_internal": 1000 * len(unresolved) // max(1, internal),
                },
                [e.evidence for e in unresolved],
            )
        )
    if ambiguous:
        statements.append(
            _statement(
                "unknown",
                "dependencies.ambiguous",
                {"edges": len(ambiguous)},
                [e.evidence for e in ambiguous],
            )
        )
    modules = Counter(e.module for e in unresolved)
    tables = (
        _table(
            "hubs_imported",
            ("file", "importers"),
            [(h.node.removeprefix("file:"), h.count) for h in a.hubs_in],
        ),
        _table(
            "hubs_importing",
            ("file", "dependencies"),
            [(h.node.removeprefix("file:"), h.count) for h in a.hubs_out],
        ),
        _table(
            "external_packages",
            ("package", "importing_files"),
            [(k.removeprefix("ext:"), n) for k, n in a.external],
        ),
        _table(
            "unresolved_modules",
            ("module", "edges"),
            [(m[:256], n) for m, n in sorted(modules.items(), key=lambda x: (-x[1], x[0]))],
        ),
    )
    return SectionV1(name="dependencies", statements=tuple(statements), tables=tables)


def _api(files: Sequence[FileEntry]) -> SectionV1:
    rows: list[tuple[Scalar, ...]] = []
    evidence: list[EvidenceRefV1] = []
    frameworks: Counter[str] = Counter()
    for f in files:
        for r in f.facts.routes:
            rows.append(
                (r.framework, r.method, _clip(r.path), _clip(r.handler), f.path, r.range.start_line)
            )
            evidence.append(_ref(f, "route", r.range))
            frameworks[r.framework] += 1
    statements = [_statement("derivation", "api.routes", {"count": len(rows)}, evidence)]
    for name, n in sorted(frameworks.items()):
        statements.append(
            _statement("derivation", "api.framework", {"framework": name, "routes": n})
        )
    return SectionV1(
        name="api_surface",
        statements=tuple(statements),
        tables=(
            _table("routes", ("framework", "method", "path", "handler", "file", "line"), rows),
        ),
    )


def _data(files: Sequence[FileEntry]) -> SectionV1:
    rows: list[tuple[Scalar, ...]] = []
    evidence: list[EvidenceRefV1] = []
    for f in files:
        for m in f.facts.models:
            rows.append(
                (m.framework, m.name, _clip(m.table), len(m.columns), f.path, m.range.start_line)
            )
            evidence.append(_ref(f, "model", m.range))
    return SectionV1(
        name="data_model",
        statements=(_statement("derivation", "data.models", {"count": len(rows)}, evidence),),
        tables=(
            _table("models", ("framework", "model", "table", "columns", "file", "line"), rows),
        ),
    )


def _config(files: Sequence[FileEntry]) -> SectionV1:
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    evidence: list[EvidenceRefV1] = []
    for f in files:
        for c in f.facts.config:
            seen[(c.kind, c.name)].add(f.path)
            evidence.append(_ref(f, "config", c.range))
    rows = [(k, n, len(p)) for (k, n), p in sorted(seen.items(), key=lambda x: (-len(x[1]), x[0]))]
    return SectionV1(
        name="configuration",
        statements=(_statement("derivation", "config.names", {"count": len(rows)}, evidence),),
        tables=(_table("config_reads", ("kind", "name", "files"), rows),),
    )


def _entries(files: Sequence[FileEntry], a: Analytics) -> SectionV1:
    by_path = {f"file:{f.path}": f for f in files}
    statements = []
    for node in a.entry_candidates[:20]:
        entry = by_path[node]
        statements.append(
            _statement(
                "inference", "entry.candidate", {"file": entry.path}, [_ref(entry, "file", None)]
            )
        )
    route_files = sorted({f.path for f in files if f.facts.routes})
    statements.append(_statement("derivation", "entry.route_files", {"count": len(route_files)}))
    return SectionV1(name="entry_points", statements=tuple(statements))


LIMITATIONS = (
    "limitation.syntax_only",
    "limitation.aliases_and_build_configuration",
    "limitation.no_manifests",
    "limitation.config_values_not_recorded",
    "limitation.component_grouping_is_heuristic",
)


def _coverage(files: Sequence[FileEntry], cov: CoverageV1, a: Analytics) -> SectionV1:
    statements = [
        _statement(
            "derivation",
            "coverage.files",
            {
                "eligible": cov.eligible_files,
                "analyzed": cov.analyzed_files,
                "partial": cov.partial_files,
                "omitted": cov.omitted_files,
            },
        )
    ]
    if not cov.complete:
        statements.append(
            _statement(
                "unknown",
                "coverage.truncated",
                {
                    "unanalyzed": cov.eligible_files - cov.analyzed_files,
                    "omitted": cov.omitted_files,
                },
            )
        )
    partial = [f for f in files if f.facts.status == "partial"]
    if partial:
        statements.append(
            _statement(
                "unknown",
                "coverage.partial_files",
                {"count": len(partial)},
                [_ref(f, "file", None) for f in partial],
            )
        )
    if a.disposition != "complete":
        statements.append(_statement("unknown", "analytics.incomplete", {"reason": a.disposition}))
    statements.extend(_statement("unknown", code) for code in LIMITATIONS)
    return SectionV1(name="coverage", statements=tuple(statements))


def assemble_report(
    *,
    org_id: UUID,
    repository_id: UUID,
    snapshot_id: UUID,
    files: Sequence[FileEntry],
    inventory: Iterable[str],
    eligible_files: int,
    omitted_files: int,
    fact_runs: tuple[UUID, ...],
) -> ArchitectureReportV1:
    ordered = sorted(files, key=lambda f: f.path)
    graph = build_graph(ordered, inventory)
    analytics = analyze(graph)
    partial = sum(1 for f in ordered if f.facts.status == "partial")
    coverage = CoverageV1(
        eligible_files=max(eligible_files, len(ordered)),
        analyzed_files=len(ordered),
        partial_files=partial,
        omitted_files=omitted_files,
        fact_runs=tuple(sorted(fact_runs)),
    )
    digests = tuple(FileFactsV1.facts_sha256(f.facts) for f in ordered)
    report = ArchitectureReportV1(
        org_id=org_id,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        inputs_digest=inputs_digest(fact_runs, digests),
        coverage=coverage,
        graph=graph,
        sections=(
            _overview(ordered, graph, _frameworks(ordered, graph)),
            _components(analytics),
            _dependencies(ordered, graph, analytics),
            _api(ordered),
            _data(ordered),
            _config(ordered),
            _entries(ordered, analytics),
            _coverage(ordered, coverage, analytics),
        ),
    )
    report.check_size()
    return report
