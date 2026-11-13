"""Evidence-first architecture report and dependency graph contracts.

Every statement is typed as an observation (read directly from a fact), a derivation (computed
mechanically from observations), an inference (a judgment that is not proven by the facts) or an
unknown (something the facts cannot settle). Unresolved is unknown, never absent. A statement is a
template code plus scalar parameters, so text is rendered deterministically and never carries source
values or quotes; evidence points at files and ranges instead.
"""

import hashlib
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.domain.scope import Contract
from app.domain.source_ranges import SourceRangeV1

MAX_NODES = 20_000
MAX_EDGES = 100_000
MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_FACT_RUNS = 64


class ReportTooLarge(ValueError):  # noqa: N818
    """The repository exceeds a documented report bound; the report is refused, not truncated."""


StatementKind = Literal["observation", "derivation", "inference", "unknown"]
Resolution = Literal["resolved", "ambiguous", "external", "unresolved"]
FactKind = Literal["file", "import", "export", "route", "model", "config", "symbol"]
Scalar = str | int | float | bool
SectionName = Literal[
    "overview",
    "components",
    "dependencies",
    "api_surface",
    "data_model",
    "configuration",
    "entry_points",
    "coverage",
]


class EvidenceRefV1(Contract):
    file_id: UUID
    path: str = Field(min_length=1, max_length=1024)
    fact: FactKind
    range: SourceRangeV1 | None = None


class StatementV1(Contract):
    kind: StatementKind
    code: str = Field(min_length=1, max_length=96, pattern=r"^[a-z][a-z0-9_.]*$")
    params: dict[str, Scalar] = Field(default_factory=dict, max_length=16)
    evidence: tuple[EvidenceRefV1, ...] = Field(default=(), max_length=8)
    evidence_total: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def default_total(self) -> Self:
        # Unless a caller says more evidence exists than is shown, the total is what is shown.
        if self.evidence_total is None:
            object.__setattr__(self, "evidence_total", len(self.evidence))
        return self

    @field_validator("params", mode="before")
    @classmethod
    def clip_values(cls, value: Any) -> Any:
        """Paths and names can be long; clip them deterministically instead of failing a build."""
        if isinstance(value, dict):
            return {k: (v[:512] if isinstance(v, str) else v) for k, v in value.items()}
        return value

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if any(not 1 <= len(k) <= 64 for k in self.params):
            raise ValueError("parameter names must be 1-64 characters")
        if any(isinstance(v, str) and len(v) > 512 for v in self.params.values()):
            raise ValueError("parameter values are bounded")
        if self.evidence_total is not None and self.evidence_total < len(self.evidence):
            raise ValueError("evidence_total cannot be below the evidence shown")
        # An observation must point at something; an unknown may not claim to be observed.
        if self.kind == "observation" and not self.evidence:
            raise ValueError("an observation needs evidence")
        return self


class TableV1(Contract):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    columns: tuple[str, ...] = Field(min_length=1, max_length=12)
    rows: tuple[tuple[Scalar, ...], ...] = Field(default=(), max_length=200)
    rows_total: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def rectangular(self) -> Self:
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("table rows must match the columns")
        if self.rows_total < len(self.rows):
            raise ValueError("rows_total cannot be below the rows shown")
        return self


class SectionV1(Contract):
    name: SectionName
    statements: tuple[StatementV1, ...] = Field(default=(), max_length=200)
    tables: tuple[TableV1, ...] = Field(default=(), max_length=8)


class GraphNodeV1(Contract):
    id: str = Field(min_length=1, max_length=1100)
    kind: Literal["file", "package", "external"]
    label: str = Field(min_length=1, max_length=1024)
    language: str | None = Field(default=None, max_length=64)
    file_id: UUID | None = None


class GraphEdgeV1(Contract):
    source: str = Field(min_length=1, max_length=1100)
    target: str | None = Field(default=None, max_length=1100)
    candidates: tuple[str, ...] = Field(default=(), max_length=8)
    module: str = Field(max_length=512)
    import_kind: str = Field(min_length=1, max_length=32)
    resolution: Resolution
    evidence: EvidenceRefV1

    @model_validator(mode="after")
    def coherent(self) -> Self:
        resolved = self.resolution in {"resolved", "external"}
        if resolved != (self.target is not None):
            raise ValueError("only resolved and external edges have a target")
        if (self.resolution == "ambiguous") != (len(self.candidates) >= 2):
            raise ValueError("an ambiguous edge lists at least two candidates, others none")
        return self


class DependencyGraphV1(Contract):
    nodes: tuple[GraphNodeV1, ...] = Field(default=(), max_length=MAX_NODES)
    edges: tuple[GraphEdgeV1, ...] = Field(default=(), max_length=MAX_EDGES)

    @model_validator(mode="after")
    def closed(self) -> Self:
        ids = [n.id for n in self.nodes]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate graph node id")
        known = set(ids)
        for edge in self.edges:
            if edge.source not in known or (edge.target is not None and edge.target not in known):
                raise ValueError("edge endpoint is not a graph node")
            if any(c not in known for c in edge.candidates):
                raise ValueError("edge candidate is not a graph node")
        return self

    def counts(self) -> dict[str, int]:
        result = {"resolved": 0, "ambiguous": 0, "external": 0, "unresolved": 0}
        for edge in self.edges:
            result[edge.resolution] += 1
        return result


class CoverageV1(Contract):
    eligible_files: int = Field(ge=0)
    analyzed_files: int = Field(ge=0)
    partial_files: int = Field(ge=0)
    omitted_files: int = Field(ge=0)
    fact_runs: tuple[UUID, ...] = Field(default=(), max_length=MAX_FACT_RUNS)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.analyzed_files > self.eligible_files or self.partial_files > self.analyzed_files:
            raise ValueError("coverage counts are inconsistent")
        return self

    @property
    def complete(self) -> bool:
        return self.analyzed_files == self.eligible_files and self.omitted_files == 0


class RepositoryReportV1(Contract):
    schema_version: Literal[1] = 1
    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    inputs_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage: CoverageV1
    graph: DependencyGraphV1
    sections: tuple[SectionV1, ...] = Field(max_length=len(SectionName.__args__))  # type: ignore[attr-defined]

    @model_validator(mode="after")
    def unique_sections(self) -> Self:
        names = [s.name for s in self.sections]
        if len(set(names)) != len(names):
            raise ValueError("duplicate report section")
        return self

    def digest(self) -> str:
        return semantic_digest(self.model_dump(mode="json"))

    def size_bytes(self) -> int:
        return len(canonical_bytes(self.model_dump(mode="json")))

    def check_size(self) -> None:
        if self.size_bytes() > MAX_REPORT_BYTES:
            raise ReportTooLarge("report exceeds the size bound")


def inputs_digest(run_ids: tuple[UUID, ...], facts_digests: tuple[str, ...]) -> str:
    """Identity of a report's inputs: the fact runs and the digest of every file's facts."""
    material = "\n".join([*(str(r) for r in sorted(run_ids)), *sorted(facts_digests)])
    return hashlib.sha256(material.encode()).hexdigest()


ShardState = Literal[
    "not_started", "queued", "running", "retry_wait", "cancelling", "succeeded", "partial",
    "failed", "cancelled",
]  # fmt: skip


class ShardStatusV1(Contract):
    index: int = Field(ge=0)
    files: int = Field(ge=0)
    prefixes: int = Field(ge=0)
    state: ShardState
    job_id: UUID | None = None
    run_id: UUID | None = None


class ArchitectureShardsV1(Contract):
    schema_version: Literal[1] = 1
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    total_files: int = Field(ge=0)
    skipped_files: int = Field(ge=0)
    shards: tuple[ShardStatusV1, ...] = Field(default=(), max_length=1000)
    ready: bool  # every shard finished: a report can be built


class ArchitectureStartRequestV1(Contract):
    schema_version: Literal[1] = 1
    request_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def reserved(self) -> Self:
        if self.request_key.startswith(("job:", "child:")):
            raise ValueError("invalid request key")
        return self


class ArchitectureReportRefV1(Contract):
    schema_version: Literal[1] = 1
    id: UUID
    repository_id: UUID
    snapshot_id: UUID
    inputs_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    complete: bool
    analyzed_files: int = Field(ge=0)
    eligible_files: int = Field(ge=0)


class ArchitectureTemplatesV1(Contract):
    """Statement text templates keyed by statement code (``{name}`` marks a parameter)."""

    schema_version: Literal[1] = 1
    templates: dict[str, str]


class GraphNodeOutV1(Contract):
    id: str
    label: str
    kind: Literal["file", "package", "external"]
    language: str | None = None
    group: str | None = None


class GraphLinkOutV1(Contract):
    source: str
    target: str
    resolution: Resolution


class ArchitectureGraphPageV1(Contract):
    """A slice of the graph in D3's nodes-and-links shape; unknown edges are only counted."""

    schema_version: Literal[1] = 1
    nodes: tuple[GraphNodeOutV1, ...]
    links: tuple[GraphLinkOutV1, ...]
    nodes_total: int = Field(ge=0)
    links_total: int = Field(ge=0)
    offset: int = Field(ge=0)
    ambiguous: int = Field(ge=0)
    unresolved: int = Field(ge=0)
