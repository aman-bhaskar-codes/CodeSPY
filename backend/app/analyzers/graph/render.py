# ruff: noqa: E501
"""Deterministic Markdown and graph payload for an architecture report."""

from typing import Any

from app.analyzers.graph.analytics import analyze
from app.contracts.architecture_report import (
    ArchitectureGraphPageV1,
    EvidenceRefV1,
    GraphLinkOutV1,
    GraphNodeOutV1,
    RepositoryReportV1,
    SectionV1,
    StatementV1,
    TableV1,
)

TEXT: dict[str, str] = {
    "overview.files": "{analyzed} source files were analyzed.",
    "overview.symbols": "{total} symbols were extracted.",
    "overview.primary_language": "The most common language is {language} ({files} files).",
    "stack.framework": "{framework} appears to be used (imported by {files} files).",
    "components.count": "The code groups into {count} components.",
    "components.layers": "The component import graph has {layers} layers.",
    "components.grouping_heuristic": "Components are grouped by directory structure, at most {max_components}.",
    "components.cycle": "{size} components import each other in a cycle: {members}.",
    "dependencies.counts": "Imports: {resolved} resolved, {ambiguous} ambiguous, {external} external, {unresolved} unresolved.",
    "dependencies.external": "{packages} external packages are imported.",
    "dependencies.cycle": "{size} files import each other in a cycle: {members}.",
    "dependencies.top_imported": "{node} is the most imported file ({count} importers).",
    "dependencies.unresolved": "{edges} imports could not be located ({permille_of_internal} per mille of internal imports); their targets are unknown.",
    "dependencies.ambiguous": "{edges} imports match several files equally well; the target is unknown.",
    "api.routes": "{count} routes were found.",
    "api.framework": "{routes} routes use {framework}.",
    "data.models": "{count} data models were found.",
    "config.names": "{count} distinct environment variables or configuration keys are read (names only).",
    "entry.candidate": "{file} looks like an entry point (named like one and imported by nothing).",
    "entry.route_files": "{count} files declare routes.",
    "coverage.files": "Coverage: {analyzed} of {eligible} eligible files analyzed ({partial} partially, {omitted} omitted).",
    "coverage.truncated": "Not every eligible file was analyzed ({unanalyzed} not analyzed, {omitted} omitted); statements about the rest are unknown.",
    "coverage.partial_files": "{count} files had syntax errors; their facts are partial.",
    "analytics.incomplete": "A bounded computation stopped early ({reason}); derived structure may be incomplete.",
    "limitation.syntax_only": "Facts are syntactic: names are not resolved across files beyond the rules in this report.",
    "limitation.aliases_and_build_configuration": "Path aliases, bundler settings, go.mod and build-system classpaths are not read, so such imports are unknown.",
    "limitation.no_manifests": "Package manifests are not parsed; the technology stack is inferred from imports and framework facts.",
    "limitation.config_values_not_recorded": "Only configuration names are recorded, never values.",
    "limitation.component_grouping_is_heuristic": "Component boundaries are a directory-based heuristic, not a declared architecture.",
}

BADGE = {
    "observation": "OBSERVED",
    "derivation": "DERIVED",
    "inference": "INFERRED",
    "unknown": "UNKNOWN",
}
TITLE = {
    "overview": "Overview",
    "components": "Components",
    "dependencies": "Dependencies",
    "api_surface": "API surface",
    "data_model": "Data model",
    "configuration": "Configuration",
    "entry_points": "Entry points",
    "coverage": "Coverage and limitations",
}


class _Params(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "?"


def _clean(value: object) -> str:
    """One line of plain text: control characters and line separators become spaces."""
    return "".join(" " if ord(c) < 32 or c in "\x7f\u2028\u2029\x85" else c for c in str(value))


def statement_text(statement: StatementV1) -> str:
    template = TEXT.get(statement.code)
    if template is None:
        pairs = ", ".join(f"{k}={v}" for k, v in sorted(statement.params.items()))
        return f"{statement.code}" + (f" ({pairs})" if pairs else "")
    return _clean(template.format_map(_Params(statement.params)))


def _location(ref: EvidenceRefV1) -> str:
    return f"{ref.path}:{ref.range.start_line}" if ref.range else ref.path


def _cell(value: object) -> str:
    return _clean(value).replace("|", "\\|")


def _table(table: TableV1) -> list[str]:
    if not table.rows:
        return []
    lines = [f"**{table.name.replace('_', ' ')}**", "", "| " + " | ".join(table.columns) + " |"]
    lines.append("|" + "|".join(" --- " for _ in table.columns) + "|")
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in table.rows]
    if table.rows_total > len(table.rows):
        lines.append(f"\n_{table.rows_total - len(table.rows)} more rows not shown._")
    return [*lines, ""]


def _section(section: SectionV1) -> list[str]:
    lines = [f"## {TITLE[section.name]}", ""]
    for s in section.statements:
        refs = ", ".join(_location(r) for r in s.evidence[:3])
        more = (s.evidence_total or len(s.evidence)) - min(3, len(s.evidence))
        evidence = f" (evidence: {refs}{f' and {more} more' if more > 0 else ''})" if refs else ""
        lines.append(f"- **{BADGE[s.kind]}** {statement_text(s)}{evidence}")
    lines.append("")
    for table in section.tables:
        lines += _table(table)
    return lines


def render_markdown(report: RepositoryReportV1) -> str:
    cov = report.coverage
    lines = ["# Architecture report", ""]
    if not cov.complete:
        lines += [
            f"> **Partial coverage:** {cov.analyzed_files} of {cov.eligible_files} eligible files "
            "were analyzed. Everything below describes only those files.",
            "",
        ]
    lines += [
        f"Report digest: `{report.digest()}`  ",
        f"Inputs digest: `{report.inputs_digest}`",
        "",
    ]
    order = list(TITLE)
    for section in sorted(report.sections, key=lambda s: order.index(s.name)):
        lines += _section(section)
    return "\n".join(lines).rstrip() + "\n"


def graph_payload(report: RepositoryReportV1) -> dict[str, Any]:
    """D3-compatible ``{nodes, links}`` for resolved and external edges; unknowns are counted."""
    components = analyze(report.graph).component_by_file
    counts = report.graph.counts()
    return {
        "nodes": [
            {
                "id": n.id,
                "label": n.label,
                "kind": n.kind,
                "language": n.language,
                "group": components.get(n.id),
            }
            for n in report.graph.nodes
        ],
        "links": [
            {"source": e.source, "target": e.target, "resolution": e.resolution}
            for e in report.graph.edges
            if e.target is not None
        ],
        "unknown": {"ambiguous": counts["ambiguous"], "unresolved": counts["unresolved"]},
    }


def graph_page(
    report: RepositoryReportV1, offset: int = 0, limit: int = 2000
) -> ArchitectureGraphPageV1:
    """A bounded slice of the graph payload; nodes and links page independently by ``offset``."""
    payload = graph_payload(report)
    nodes, links = payload["nodes"], payload["links"]
    return ArchitectureGraphPageV1(
        nodes=tuple(GraphNodeOutV1(**n) for n in nodes[offset : offset + limit]),
        links=tuple(GraphLinkOutV1(**link) for link in links[offset : offset + limit]),
        nodes_total=len(nodes),
        links_total=len(links),
        offset=offset,
        ambiguous=payload["unknown"]["ambiguous"],
        unresolved=payload["unknown"]["unresolved"],
    )
