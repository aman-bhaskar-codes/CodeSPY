from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.contracts.evidence import EvidenceEnvelopeV1
from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.domain.scope import Contract, RunRef
from app.domain.source_ranges import SourceRangeV1

Hash = str


class RepositoryCreate(Contract):
    repository_key: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=200)
    source_kind: Literal["local_fixture", "github", "archive"]


class ArtifactCreate(Contract):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    storage_key: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def opaque_key(self) -> Self:
        if len(self.storage_key.encode()) > 1024 or any(
            x in self.storage_key for x in ("://", "?", "#", "\x00")
        ):
            raise ValueError("opaque artifact key required")
        return self


class SourceFileCreate(Contract):
    id: UUID
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    language: str | None = None

    @model_validator(mode="after")
    def safe_path(self) -> Self:
        if (
            not self.path
            or len(self.path.encode()) > 1024
            or "\\" in self.path
            or "\x00" in self.path
            or any(p in ("", ".", "..") for p in self.path.split("/"))
        ):
            raise ValueError("relative artifact path required")
        return self


class SnapshotCreate(Contract):
    repository_id: UUID
    artifact: ArtifactCreate
    source_revision: str | None = Field(default=None, min_length=1, max_length=255)
    admission_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class RunCreate(Contract):
    snapshot_id: UUID
    request_key: str = Field(min_length=1, max_length=128)
    analyzer_name: str = Field(min_length=1, max_length=128)
    analyzer_version: str = Field(min_length=1, max_length=128)
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if len(canonical_bytes(self.config)) > 65536:
            raise ValueError("configuration exceeds bound")
        return self


class NodeCreateV1(Contract):
    local_id: UUID
    file_id: UUID
    node_kind: Literal["module", "class", "function", "method", "call", "import"]
    symbol_name: str = Field(max_length=2048)
    qualified_name: str = Field(max_length=2048)
    range: SourceRangeV1
    attributes: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if len(canonical_bytes(self.attributes)) > 65536:
            raise ValueError("attributes exceed bound")
        return self


class EdgeCreateV1(Contract):
    id: UUID
    source_local_id: UUID
    target_local_id: UUID
    edge_kind: Literal["AST_CHILD", "IMPORTS", "CALLS_CANDIDATE"]
    support_evidence_id: UUID
    properties: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if len(canonical_bytes(self.properties)) > 65536:
            raise ValueError("properties exceed bound")
        return self


class DerivationCreateV1(Contract):
    child_id: UUID
    parent_id: UUID


class CoverageV1(Contract):
    status: Literal["complete", "partial", "unknown", "unavailable"]
    scope_description: str = Field(min_length=1, max_length=2048)
    admitted_count: int = Field(ge=0)
    processed_count: int = Field(ge=0)
    omitted_count: int = Field(ge=0)
    truncated: bool

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.processed_count + self.omitted_count != self.admitted_count:
            raise ValueError("coverage counts disagree")
        if self.status == "complete" and (self.omitted_count or self.truncated):
            raise ValueError("complete coverage cannot omit or truncate")
        return self


class ResultBatchV1(Contract):
    schema_version: Literal[1] = 1
    scope: RunRef
    nodes: list[NodeCreateV1] = Field(default_factory=list, max_length=1000)
    evidence: list[EvidenceEnvelopeV1] = Field(default_factory=list, max_length=1000)
    edges: list[EdgeCreateV1] = Field(default_factory=list, max_length=2000)
    derivations: list[DerivationCreateV1] = Field(default_factory=list, max_length=2000)
    disposition: Literal["completed", "partial"]
    coverage: CoverageV1
    limitations: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def valid_batch(self) -> Self:
        if (self.disposition == "completed") != (self.coverage.status == "complete"):
            raise ValueError("disposition and coverage disagree")
        if any(e.scope != self.scope for e in self.evidence):
            raise ValueError("foreign evidence scope")
        for records, key in ((self.nodes, "local_id"), (self.evidence, "id"), (self.edges, "id")):
            ids = [getattr(r, key) for r in records]
            if len(set(ids)) != len(ids):
                raise ValueError("duplicate record identity")
        children: dict[UUID, list[UUID]] = {}
        for d in self.derivations:
            children.setdefault(d.parent_id, []).append(d.child_id)
        active: set[UUID] = set()
        done: set[UUID] = set()
        for node in children:
            stack = [(node, False)]
            while stack:
                n, exit_node = stack.pop()
                if exit_node:
                    active.discard(n)
                    done.add(n)
                    continue
                if n in active:
                    raise ValueError("derivation cycle")
                if n in done:
                    continue
                active.add(n)
                stack.append((n, True))
                stack.extend((c, False) for c in children.get(n, []))
        return self

    def semantic_hash(self) -> str:
        data = self.model_dump(exclude={"nodes", "evidence", "edges", "derivations"})
        data["nodes"] = [n.model_dump() for n in sorted(self.nodes, key=lambda n: str(n.local_id))]
        data["evidence"] = [
            {"id": str(e.id), "digest": e.semantic_hash()}
            for e in sorted(self.evidence, key=lambda e: str(e.id))
        ]
        data["edges"] = [e.model_dump() for e in sorted(self.edges, key=lambda e: str(e.id))]
        data["derivations"] = [
            d.model_dump()
            for d in sorted(self.derivations, key=lambda d: (str(d.child_id), str(d.parent_id)))
        ]
        return semantic_digest(data)


class RepositoryRecord(RepositoryCreate):
    org_id: UUID
    id: UUID
    created_at: datetime


class SnapshotRecord(Contract):
    org_id: UUID
    id: UUID
    repository_id: UUID
    artifact_id: UUID
    source_revision: str | None
    admission_config_hash: str
    identity_hash: str
    manifest_hash: str | None
    state: Literal["building", "sealed"]
    created_at: datetime
    sealed_at: datetime | None
    file_count: int


class RunRecord(Contract):
    org_id: UUID
    snapshot_id: UUID
    id: UUID
    request_key: str
    analyzer_name: str
    analyzer_version: str
    config_hash: str
    config: dict[str, Any]
    status: str
    write_version: int
    coverage: dict[str, Any]
    limitations: list[str]
    result_hash: str | None
    failure_code: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @property
    def ref(self) -> RunRef:
        return RunRef(org_id=self.org_id, snapshot_id=self.snapshot_id, run_id=self.id)

    @property
    def request(self) -> RunCreate:
        return RunCreate(
            snapshot_id=self.snapshot_id,
            request_key=self.request_key,
            analyzer_name=self.analyzer_name,
            analyzer_version=self.analyzer_version,
            config=self.config,
        )


class InsertSummary(Contract):
    inserted_nodes: int = Field(ge=0)
    existing_nodes: int = Field(ge=0)
    inserted_edges: int = Field(ge=0)
    existing_edges: int = Field(ge=0)


class NeighborPage(Contract):
    scope: RunRef
    edges: list[EdgeCreateV1]
    next_cursor: str | None = None
    truncated: bool


class GraphPage(NeighborPage):
    nodes: list[NodeCreateV1]


# RunRef is intentionally distinct from persistence row identity.
def _run_ref(record: RunRecord) -> RunRef:
    return RunRef(org_id=record.org_id, snapshot_id=record.snapshot_id, run_id=record.id)
