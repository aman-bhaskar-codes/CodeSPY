"""Deterministic text and JSON reporting for ArchitectureReportV1."""

from app.contracts.structure import ArchitectureReportV1
from app.domain.canonical_json import canonical_bytes


def render_json(report: ArchitectureReportV1) -> str:
    """Canonical JSON export of the architecture report."""
    return canonical_bytes(report.model_dump()).decode("utf-8")


def render_markdown(report: ArchitectureReportV1) -> str:
    """Deterministic human-readable Markdown summary without LLM inference."""
    size_mib = report.limits.source_bytes // (1024 * 1024)
    res_summary = (
        f"{report.coverage.resolved} resolved, "
        f"{report.coverage.ambiguous} ambiguous, "
        f"{report.coverage.unresolved} unresolved"
    )
    lines: list[str] = [
        "# CodeSPY Architecture Report",
        "",
        f"- Profile: {report.limits.source_files} max files, {size_mib} MiB max bytes",
        f"- Processed files: {report.coverage.processed_files} / {report.coverage.requested_files}",
        f"- Symbols observed: {len(report.symbols)}",
        f"- Resolutions: {res_summary}",
        f"- Routes observed: {len(report.routes)}",
        f"- HTTP client requests: {len(report.requests)}",
        f"- Boundary candidate links: {len(report.links)}",
        "",
        "## Nonclaims",
        "",
    ]
    for nonclaim in report.nonclaims:
        lines.append(f"> {nonclaim}")
    lines.append("")

    if report.routes:
        lines.append("## Registered Routes")
        lines.append("")
        lines.append("| Framework | Method | Path | State |")
        lines.append("| :--- | :--- | :--- | :--- |")
        for route in report.routes:
            method = route.method
            path = route.template_path or route.raw_path or "<unknown>"
            lines.append(f"| {route.framework} | {method} | `{path}` | {route.state} |")
        lines.append("")

    if report.links:
        lines.append("## Network Boundary Links")
        lines.append("")
        lines.append("| State | Reason |")
        lines.append("| :--- | :--- |")
        for link in report.links:
            lines.append(f"| {link.state} | {link.reason} |")
        lines.append("")

    if report.diagnostics:
        lines.append("## Diagnostics")
        lines.append("")
        for diag in report.diagnostics:
            lines.append(f"- [{diag.severity.upper()}] {diag.code}: {diag.message}")
        lines.append("")

    return "\n".join(lines)
