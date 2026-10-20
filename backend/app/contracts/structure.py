"""Pure, frozen structural transport. Semantic keys precede run-scoped identities."""

from collections.abc import Callable
from hashlib import sha256
from time import monotonic
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.contracts.ingestion import AdmissionPolicyV1
from app.contracts.persistence import ResultBatchV1
from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.domain.scope import Contract, RunRef
from app.domain.source_ranges import SourceRangeV1

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Key = Hash
Language = Literal["python", "javascript", "jsx", "typescript", "tsx"]


def normalize_prefixes(values: tuple[str, ...]) -> tuple[str, ...]:
    result = set()
    for value in values:
        value = value.removesuffix("/")
        if (
            not value
            or len(value.encode()) > 1024
            or "\\" in value
            or "\0" in value
            or any(p in {"", ".", ".."} for p in value.split("/"))
        ):
            raise ValueError("relative POSIX prefix required")
        result.add(value)
    if len(result) > 16:
        raise ValueError("too many prefixes")
    return tuple(sorted(result))


class StructuralAnalysisRequestV1(Contract):
    schema_version: Literal[1] = 1
    snapshot_id: UUID
    include_prefixes: tuple[str, ...] = ()
    profile: Literal["structural-small-v1"] = "structural-small-v1"

    @model_validator(mode="after")
    def bounded(self) -> Self:
        object.__setattr__(self, "include_prefixes", normalize_prefixes(self.include_prefixes))
        if len(canonical_bytes(self.model_dump())) > 4096:
            raise ValueError("request exceeds bound")
        return self


class StructuralLimitsV1(Contract):
    source_files: int = Field(default=128, ge=1, le=128)
    source_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=16 * 1024 * 1024)
    file_bytes: int = Field(default=2 * 1024 * 1024, ge=1, le=2 * 1024 * 1024)
    config_files: int = Field(default=32, ge=0, le=32)
    config_file_bytes: int = Field(default=256 * 1024, ge=1, le=256 * 1024)
    config_bytes: int = Field(default=1024 * 1024, ge=1, le=1024 * 1024)
    syntax_nodes_per_file: int = Field(default=200000, ge=1, le=200000)
    syntax_nodes: int = Field(default=1000000, ge=1, le=1000000)
    resolution_sites: int = Field(default=2000, ge=1, le=2000)
    candidates_per_site: int = Field(default=16, ge=1, le=16)
    import_depth: int = Field(default=16, ge=1, le=16)
    module_lookups: int = Field(default=4096, ge=1, le=4096)
    nodes: int = Field(default=1000, ge=1, le=1000)
    edges: int = Field(default=2000, ge=0, le=2000)
    evidence: int = Field(default=1000, ge=1, le=1000)
    derivations: int = Field(default=2000, ge=0, le=2000)
    diagnostics: int = Field(default=500, ge=0, le=500)
    report_bytes: int = Field(default=2 * 1024 * 1024, ge=1, le=2 * 1024 * 1024)
    result_bytes: int = Field(default=8 * 1024 * 1024, ge=1, le=8 * 1024 * 1024)
    analysis_seconds: int = Field(default=180, ge=1, le=180)
    operation_seconds: int = Field(default=20, ge=1, le=20)


class ServiceBindingV1(Contract):
    source_prefix: str
    service_key: str = Field(min_length=1, max_length=128)
    canonical_origin: str = Field(min_length=1, max_length=2048)
    same_origin_service: str | None = None


class AnalysisConfigV1(Contract):
    schema_version: Literal[1] = 1
    profile: Literal["structural-small-v1"] = "structural-small-v1"
    include_prefixes: tuple[str, ...] = ()
    adapters: tuple[Literal["python", "js_ts", "fastapi", "express", "http_client"], ...] = (
        "python",
        "js_ts",
        "fastapi",
        "express",
        "http_client",
    )
    resolution_profile: Literal["standard", "syntax_only"] = "standard"
    toolchain_hash: Hash
    limits: StructuralLimitsV1 = StructuralLimitsV1()
    service_bindings: tuple[ServiceBindingV1, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        object.__setattr__(self, "include_prefixes", normalize_prefixes(self.include_prefixes))
        if len(canonical_bytes(self.model_dump())) > 65536:
            raise ValueError("configuration exceeds bound")
        return self

    def semantic_hash(self) -> str:
        return semantic_digest(self.model_dump())


class AnalysisSpecV1(Contract):
    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    id: UUID
    principal_id: UUID
    config: AnalysisConfigV1
    config_hash: Hash
    toolchain_hash: Hash


class SourceDescriptorV1(Contract):
    path: str = Field(min_length=1, max_length=4096)
    sha256: Hash
    size_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def generated_filename(self) -> Self:
        if self.path not in {"archive.zip", "manifest.json"}:
            raise ValueError("generated source input filename required")
        return self


class SourcePolicyV1(Contract):
    strip_single_root: bool = False
    policy_hash: Hash
    admission: AdmissionPolicyV1 = AdmissionPolicyV1()

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (
            self.policy_hash != self.admission.digest()
            or self.strip_single_root != self.admission.github_strip_root
        ):
            raise ValueError("original admission policy mismatch")
        return self


class SourceFileV1(Contract):
    file_id: UUID
    path: str = Field(min_length=1, max_length=1024)
    sha256: Hash
    size_bytes: int = Field(ge=0)
    eligible: bool
    exclusion_reason: str | None = None
    language: str | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        normalize_prefixes((self.path,))
        if self.path.endswith("/") or self.eligible != (self.exclusion_reason is None):
            raise ValueError("source metadata invalid")
        return self


class SourceInputV1(Contract):
    archive: SourceDescriptorV1
    manifest: SourceDescriptorV1
    files: tuple[SourceFileV1, ...] = Field(max_length=10000)
    policy: SourcePolicyV1
    config: AnalysisConfigV1
    config_hash: Hash
    toolchain_hash: Hash
    request_nonce: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (
            self.config_hash != self.config.semantic_hash()
            or self.toolchain_hash != self.config.toolchain_hash
        ):
            raise ValueError("source configuration mismatch")
        if len({f.file_id for f in self.files}) != len(self.files) or len(
            {f.path for f in self.files}
        ) != len(self.files):
            raise ValueError("duplicate source identity")
        if any(f.eligible != (f.exclusion_reason is None) for f in self.files):
            raise ValueError("source eligibility mismatch")
        return self


class SourceUnitV1(Contract):
    file_id: UUID
    path: str
    sha256: Hash
    data: bytes = Field(max_length=2 * 1024 * 1024)
    language: Language

    @model_validator(mode="after")
    def coherent(self) -> Self:
        normalize_prefixes((self.path,))
        if self.path.endswith("/") or sha256(self.data).hexdigest() != self.sha256:
            raise ValueError("source bytes or path mismatch")
        return self


class SourceLocationV1(Contract):
    file_id: UUID
    sha256: Hash
    range: SourceRangeV1


class SymbolObservationV1(Contract):
    semantic_key: Key
    kind: Literal["module", "class", "function", "method", "call", "import"]
    path: str = Field(min_length=1, max_length=1024)
    sha256: Hash
    location: SourceLocationV1
    name: str = Field(max_length=2048)
    qualified_name: str = Field(max_length=2048)
    container_key: Key | None = None
    role: Literal["declaration", "call", "import"]
    is_async: bool = False
    exported: bool = False
    type_only: bool = False

    @property
    def file_id(self) -> UUID:
        return self.location.file_id


class ResolutionV1(Contract):
    site_key: Key
    resolution_kind: Literal["binding", "call_target", "import"]
    namespace: Literal["value", "type"] = "value"
    status: Literal["resolved", "ambiguous", "unresolved"]
    candidate_keys: tuple[Key, ...] = Field(default=(), max_length=16)
    reason: str = Field(default="unknown", max_length=128)
    basis: Literal["lexical", "import_alias", "compiler_symbol", "static_member", "unsupported"] = (
        "unsupported"
    )
    external_reference: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def shape(self) -> Self:
        count = len(self.candidate_keys)
        if (
            len(set(self.candidate_keys)) != count
            or (self.status == "resolved" and count != 1)
            or (self.status == "ambiguous" and count < 2)
            or (self.status == "unresolved" and count)
        ):
            raise ValueError("resolution candidates contradict status")
        return self


class DiagnosticV1(Contract):
    code: str = Field(min_length=1, max_length=128)
    severity: Literal["info", "warning", "error"] = "warning"
    location: SourceLocationV1 | None = None
    message: str = Field(max_length=256)
    stage: str = Field(max_length=64)
    affected_count: int = Field(default=1, ge=0)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if len(self.message.encode()) > 256:
            raise ValueError("diagnostic message exceeds byte bound")
        return self


Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ANY", "UNKNOWN"]


class RouteObservationV1(Contract):
    key: Key
    framework: Literal["fastapi", "express"]
    service_key: str | None = None
    method: Method = "UNKNOWN"
    raw_path: str | None = Field(default=None, max_length=2048)
    template_path: str | None = Field(default=None, max_length=2048)
    registration: SourceLocationV1
    handler: SourceLocationV1 | None = None
    includes: tuple[SourceLocationV1, ...] = Field(default=(), max_length=16)
    handler_key: Key | None = None
    state: Literal["resolved", "ambiguous", "unresolved"] = "unresolved"
    evidence_keys: tuple[Key, ...] = Field(default=(), max_length=32)
    limitations: tuple[str, ...] = Field(default=(), max_length=32)


class HttpRequestObservationV1(Contract):
    key: Key
    origin_state: Literal["known", "relative", "unknown"] = "unknown"
    method: Method = "UNKNOWN"
    base_url: str | None = Field(default=None, max_length=2048)
    path: str | None = Field(default=None, max_length=2048)
    template_segments: tuple[str, ...] = Field(default=(), max_length=128)
    location: SourceLocationV1
    client: Literal["fetch", "axios"]
    evidence_keys: tuple[Key, ...] = Field(default=(), max_length=32)
    limitations: tuple[str, ...] = Field(default=(), max_length=32)


class BoundaryLinkV1(Contract):
    request_key: Key
    route_keys: tuple[Key, ...] = Field(default=(), max_length=16)
    state: Literal["candidate", "ambiguous", "unmatched"]
    reason: str = Field(max_length=128)
    evidence_keys: tuple[Key, ...] = Field(default=(), max_length=32)
    limitations: tuple[str, ...] = Field(default=(), max_length=32)


SectionState = Literal["complete", "partial", "unavailable", "not_requested"]


class StructuralCoverageV1(Contract):
    requested_files: int = Field(default=0, ge=0)
    processed_files: int = Field(default=0, ge=0)
    omitted_files: int = Field(default=0, ge=0)
    inventoried_files: int = Field(default=0, ge=0)
    eligible_files: int = Field(default=0, ge=0)
    excluded_files: int = Field(default=0, ge=0)
    unsupported_files: int = Field(default=0, ge=0)
    out_of_scope_files: int = Field(default=0, ge=0)
    resolution_records: int = Field(default=0, ge=0)
    resolved: int = Field(default=0, ge=0)
    ambiguous: int = Field(default=0, ge=0)
    unresolved: int = Field(default=0, ge=0)
    distinct_resolution_sites: int = Field(default=0, ge=0)
    omitted_diagnostics: int = Field(default=0, ge=0)
    omitted_details_count: int = Field(default=0, ge=0)
    syntax: SectionState = "complete"
    resolution: SectionState = "complete"
    framework: SectionState = "complete"
    graph: SectionState = "complete"
    omissions: tuple["FileOmissionV1", ...] = Field(default=(), max_length=10000)

    @model_validator(mode="after")
    def reconciled(self) -> Self:
        if (
            self.processed_files + self.omitted_files != self.requested_files
            or self.resolved + self.ambiguous + self.unresolved != self.resolution_records
        ):
            raise ValueError("coverage counters do not reconcile")
        return self


class FileOmissionV1(Contract):
    file_id: UUID
    path: str
    reason: str = Field(max_length=128)


class ComponentSectionV1(Contract):
    relation: Literal["imports", "candidate_calls"]
    state: SectionState
    components: tuple[tuple[Key, ...], ...] = Field(default=(), max_length=1000)
    edges: tuple[tuple[int, int], ...] = Field(default=(), max_length=2000)
    scanned_edges: int = Field(default=0, ge=0, le=20000)
    reason: str | None = None


class IdentityMapV1(Contract):
    semantic_key: Key
    physical_id: UUID


class SourceExcerptV1(Contract):
    location: SourceLocationV1
    text: str = Field(max_length=2048)
    sha256: Hash

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if len(self.text.encode()) > 2048 or sha256(self.text.encode()).hexdigest() != self.sha256:
            raise ValueError("excerpt digest or byte bound invalid")
        return self


class StructuralDraftV1(Contract):
    schema_version: Literal[1] = 1
    request_nonce: str = Field(min_length=1, max_length=128)
    config_hash: Hash
    toolchain_hash: Hash
    archive_sha256: Hash
    policy_hash: Hash
    excerpts: tuple[SourceExcerptV1, ...] = Field(default=(), max_length=1000)
    symbols: tuple[SymbolObservationV1, ...] = Field(default=(), max_length=1000)
    resolutions: tuple[ResolutionV1, ...] = Field(default=(), max_length=2000)
    routes: tuple[RouteObservationV1, ...] = Field(default=(), max_length=256)
    requests: tuple[HttpRequestObservationV1, ...] = Field(default=(), max_length=256)
    links: tuple[BoundaryLinkV1, ...] = Field(default=(), max_length=512)
    diagnostics: tuple[DiagnosticV1, ...] = Field(default=(), max_length=500)
    components: tuple[ComponentSectionV1, ...] = Field(default=(), max_length=2)
    coverage: StructuralCoverageV1

    @model_validator(mode="after")
    def excerpts_bounded(self) -> Self:
        if sum(len(item.text.encode()) for item in self.excerpts) > 65536:
            raise ValueError("aggregate excerpt bound exceeded")
        return self


class ArchitectureReportV1(StructuralDraftV1):
    run_ref: RunRef
    config_hash: Hash
    toolchain_hash: Hash
    archive_sha256: Hash
    policy_hash: Hash
    symbol_map: tuple[IdentityMapV1, ...] = Field(default=(), max_length=1000)
    evidence_map: tuple[IdentityMapV1, ...] = Field(default=(), max_length=1000)
    excerpts: tuple[SourceExcerptV1, ...] = Field(default=(), max_length=1000)
    limits: StructuralLimitsV1 = StructuralLimitsV1()
    nonclaims: tuple[str, ...] = (
        "Static bindings and candidate edges do not establish runtime reachability or safety.",
    )

    @model_validator(mode="after")
    def byte_bound(self) -> Self:
        if len(canonical_bytes(self.model_dump())) > self.limits.report_bytes:
            raise ValueError("report exceeds canonical byte budget")
        return self


class StructuralResultV1(Contract):
    schema_version: Literal[1] = 1
    run_ref: RunRef
    batch: ResultBatchV1
    report: ArchitectureReportV1
    report_sha256: Hash
    normalized_sha256: Hash

    @model_validator(mode="after")
    def scope_matches(self) -> Self:
        if (
            self.report.run_ref != self.run_ref
            or self.batch.scope != self.run_ref
            or sha256(canonical_bytes(self.report.model_dump())).hexdigest() != self.report_sha256
            or len(canonical_bytes(self.model_dump())) > 8 * 1024 * 1024
        ):
            raise ValueError("result scope or byte budget invalid")
        return self


class AnalysisAcceptedV1(Contract):
    schema_version: Literal[1] = 1
    status_url: str = Field(pattern=r"^/api/v1/jobs/[0-9a-f-]{36}$")
    spec_id: UUID
    job_id: UUID
    snapshot_id: UUID


class AnalysisBudget:
    def __init__(
        self,
        wall_seconds: float = 20.0,
        max_syntax_nodes: int = 200000,
        cancelled: Callable[[], bool] | None = None,
        clock: Callable[[], float] = monotonic,
    ):
        self.wall_seconds = wall_seconds
        self.max_syntax_nodes = max_syntax_nodes
        self.cancelled = cancelled or (lambda: False)
        self.clock = clock
        self.start_time = clock()
        self.nodes_visited = 0

    def tick_nodes(self, count: int = 1) -> None:
        self.nodes_visited += count
        if self.nodes_visited > self.max_syntax_nodes:
            raise TimeoutError("syntax_node_budget_exceeded")
        if self.cancelled():
            raise TimeoutError("cancelled")
        if self.clock() - self.start_time > self.wall_seconds:
            raise TimeoutError("wall_time_exceeded")


class FileObservationsV1(Contract):
    file_id: UUID
    path: str
    sha256: Hash
    language: Language
    symbols: tuple[SymbolObservationV1, ...] = Field(default=(), max_length=200000)
    diagnostics: tuple[DiagnosticV1, ...] = Field(default=(), max_length=500)
    has_syntax_errors: bool = False


StructuralConfigV1 = AnalysisConfigV1
StructuralReportV1 = ArchitectureReportV1
ObservationV1 = SymbolObservationV1


def stored_config(value: Any) -> AnalysisConfigV1:
    """JSONB is decoded using JSON validation semantics, retaining strict DTO guards."""
    return AnalysisConfigV1.model_validate_json(canonical_bytes(value))


def stored_report(value: Any) -> ArchitectureReportV1:
    return ArchitectureReportV1.model_validate_json(canonical_bytes(value))


ReportSection = Literal[
    "symbols",
    "resolutions",
    "routes",
    "requests",
    "links",
    "diagnostics",
    "components",
    "symbol_map",
    "evidence_map",
    "excerpts",
    "omissions",
]


class StructuralSummaryV1(ArchitectureReportV1):
    report_sha256: Hash
    normalized_sha256: Hash
    section_counts: dict[str, int]


class StructuralSectionPageV1(Contract):
    run_ref: RunRef
    section: ReportSection
    items: list[dict[str, Any]] = Field(max_length=100)
    next_cursor: str | None = None


class GraphQueryV1(Contract):
    seeds: tuple[UUID, ...] = Field(min_length=1, max_length=16)
    edge_kind: Literal["AST_CHILD", "IMPORTS", "CALLS_CANDIDATE"] = "CALLS_CANDIDATE"
    strategy: Literal["bfs", "dfs"] = "bfs"
    direction: Literal["forward", "reverse"] = "forward"
    max_nodes: int = Field(default=100, ge=1, le=500)
    max_edges: int = Field(default=200, ge=0, le=1000)
    max_depth: int = Field(default=8, ge=0, le=16)
    max_scanned_edges: int = Field(default=2000, ge=0, le=10000)


class GraphTraversalV1(Contract):
    run_ref: RunRef
    nodes: tuple[UUID, ...] = Field(max_length=500)
    edges: tuple[UUID, ...] = Field(max_length=1000)
    truncated: bool
    reason: str | None
    scanned_edges: int
