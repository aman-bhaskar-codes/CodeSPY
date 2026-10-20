"""Bounded published report projections; no source acquisition on read paths."""

from typing import Any, get_args

from app.analyzers.identities import normalize_report
from app.contracts.structure import ArchitectureReportV1, ReportSection, StructuralSummaryV1
from app.domain.canonical_json import canonical_bytes, semantic_digest

SECTIONS = get_args(ReportSection)


def report_items(report: ArchitectureReportV1, section: str) -> Any:
    if section not in SECTIONS:
        raise ValueError("unknown report section")
    return report.coverage.omissions if section == "omissions" else getattr(report, section)


def summary(report: ArchitectureReportV1) -> StructuralSummaryV1:
    data = report.model_dump(exclude=set(SECTIONS) - {"omissions"})
    data["coverage"]["omissions"] = ()
    value = StructuralSummaryV1(
        **data,
        report_sha256=semantic_digest(report.model_dump()),
        normalized_sha256=semantic_digest(normalize_report(report)),
        section_counts={name: len(report_items(report, name)) for name in SECTIONS},
    )
    if len(canonical_bytes(value.model_dump())) > 65536:
        raise ValueError("summary capacity exceeded")
    return value
