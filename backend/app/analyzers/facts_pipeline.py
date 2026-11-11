"""Budgeted AST fact extraction producing a deterministic AstDraftV1."""

from pathlib import Path

from app.analyzers.facts import FACT_LANGUAGES, extract_facts
from app.analyzers.source import AdmittedSourceReader
from app.contracts.ast_facts import AstDraftV1, FileFactsV1
from app.contracts.structure import (
    AnalysisBudget,
    FileOmissionV1,
    SourceInputV1,
    StructuralCoverageV1,
)


def analyze_facts(
    source_input: SourceInputV1,
    root: Path,
    budget: AnalysisBudget | None = None,
    *,
    isolated_operations: bool = False,
) -> AstDraftV1:
    limits = source_input.config.limits
    actual_budget = budget or AnalysisBudget(
        wall_seconds=float(limits.analysis_seconds), max_syntax_nodes=limits.syntax_nodes
    )
    reader = AdmittedSourceReader(source_input, root)
    units = reader.read_selected()
    files_by_path = {f.path: f for f in source_input.files}

    results: list[FileFactsV1] = []
    omitted = list(reader.omitted)
    for unit in units:
        actual_budget.tick_nodes(0)
        if unit.language not in FACT_LANGUAGES:
            omitted.append((unit.path, "language_unsupported"))
            continue
        if isolated_operations:
            from app.analyzers.facts_process import extract_facts_isolated

            results.append(extract_facts_isolated(unit, actual_budget, limits.operation_seconds))
        else:
            results.append(extract_facts(unit, actual_budget))

    omissions = tuple(
        FileOmissionV1(file_id=files_by_path[path].file_id, path=path, reason=reason)
        for path, reason in omitted
        if path in files_by_path
    )
    eligible = sum(1 for f in source_input.files if f.eligible)
    coverage = StructuralCoverageV1(
        requested_files=reader.requested,
        processed_files=len(results),
        omitted_files=len(omitted),
        inventoried_files=len(source_input.files),
        eligible_files=eligible,
        excluded_files=len(source_input.files) - eligible,
        syntax="partial" if omitted or any(f.status == "partial" for f in results) else "complete",
        resolution="complete",
        framework="complete",
        graph="complete",
        omissions=omissions,
    )
    return AstDraftV1(
        request_nonce=source_input.request_nonce,
        config_hash=source_input.config_hash,
        toolchain_hash=source_input.toolchain_hash,
        archive_sha256=source_input.archive.sha256,
        policy_hash=source_input.policy.policy_hash,
        files=tuple(results),
        coverage=coverage,
    )
