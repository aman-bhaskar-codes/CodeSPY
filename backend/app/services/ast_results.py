"""Broker-side re-validation of container AST facts against the admitted archive bytes."""

from dataclasses import dataclass
from pathlib import Path

from app.analyzers.facts import FACT_UNIT_LANGUAGES
from app.analyzers.positions import SourceIndex
from app.analyzers.source import AdmittedSourceReader
from app.contracts.ast_facts import AstDraftV1, FileFactsV1
from app.contracts.persistence import CoverageV1, ResultBatchV1
from app.contracts.structure import SourceInputV1
from app.domain.errors import DuplicateConflict
from app.domain.scope import RunRef


@dataclass(frozen=True)
class AstResult:
    run_ref: RunRef
    draft: AstDraftV1
    batch: ResultBatchV1
    digests: dict[str, str]  # file_id -> facts_sha256


def _ranges(facts: FileFactsV1) -> list[object]:
    spans: list[object] = [s.range for s in facts.symbols]
    spans += [i.range for i in facts.imports]
    spans += [e.range for e in facts.exports]
    spans += [r.range for r in facts.routes]
    spans += [m.range for m in facts.models]
    spans += [c.range for c in facts.config]
    spans += [d.range for d in facts.diagnostics if d.range is not None]
    return spans


def materialize_facts(
    ref: RunRef, draft: AstDraftV1, source: SourceInputV1, root: Path
) -> AstResult:
    if (
        draft.request_nonce != source.request_nonce
        or draft.config_hash != source.config_hash
        or draft.toolchain_hash != source.toolchain_hash
        or draft.archive_sha256 != source.archive.sha256
        or draft.policy_hash != source.policy.policy_hash
    ):
        raise DuplicateConflict("AST draft identity differs from the requested source")
    reader = AdmittedSourceReader(source, root)
    units = {u.path: u for u in reader.read_selected()}
    by_path = {f.path: f for f in source.files}
    seen: set[str] = set()
    digests: dict[str, str] = {}
    for facts in draft.files:
        unit = units.get(facts.path)
        stored = by_path.get(facts.path)
        if (
            unit is None
            or stored is None
            or not stored.eligible
            or facts.file_id != stored.file_id
            or facts.sha256 != stored.sha256
            or facts.language not in FACT_UNIT_LANGUAGES
            or facts.language != unit.language
            or facts.path in seen
        ):
            raise DuplicateConflict("AST facts cite a file outside the admitted source")
        seen.add(facts.path)
        index = SourceIndex(unit.data)
        for span in _ranges(facts):
            index.validate_range(span)  # type: ignore[arg-type]
        digests[str(facts.file_id)] = facts.facts_sha256()
    coverage = draft.coverage
    if (
        coverage.processed_files != len(draft.files)
        or coverage.requested_files != reader.requested
        or coverage.inventoried_files != len(source.files)
    ):
        raise DuplicateConflict("AST coverage differs from the admitted source")
    partial = coverage.omitted_files > 0 or any(f.status == "partial" for f in draft.files)
    batch = ResultBatchV1(
        scope=ref,
        disposition="partial" if partial else "completed",
        coverage=CoverageV1(
            status="partial" if partial else "complete",
            scope_description="Static syntax facts within the ast_facts adapter",
            admitted_count=coverage.requested_files,
            processed_count=coverage.processed_files,
            omitted_count=coverage.omitted_files,
            truncated=coverage.omitted_files > 0,
        ),
        limitations=["Facts are syntactic; names are not resolved across files."],
    )
    return AstResult(run_ref=ref, draft=draft, batch=batch, digests=digests)
