"""Bounded findings drafts; transport validity does not establish execution."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.contracts.dependencies import DependencyRequestV1, DependencyResultV1, PackageQuery
from app.contracts.evidence import EvidenceEnvelopeV1, EvidenceRef
from app.contracts.scanners import Digest, RuleId, ScannerRequestV1, ScannerResultV1
from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.domain.scope import Contract, RunRef
from app.domain.source_ranges import SourceRangeV1

Category = Literal["source_pattern", "secret_format", "dependency_advisory"]
Obligation = Literal[
    "authenticated-source",
    "qualified-predicate",
    "selected-format",
    "exact-lock-query",
    "fresh-complete-advisory",
]


def required_obligations(category: Category) -> tuple[Obligation, ...]:
    if category == "source_pattern":
        return ("authenticated-source", "qualified-predicate")
    if category == "secret_format":
        return ("authenticated-source", "selected-format")
    return ("exact-lock-query", "fresh-complete-advisory")


class ScanPublicationSpecV1(Contract):
    schema_version: Literal[1] = 1
    scope: RunRef
    repository_id: UUID
    config_hash: Digest
    runtime_digest: Digest
    catalog_digest: Digest
    captured_at: AwareDatetime
    native_requests: tuple[ScannerRequestV1, ...] = Field(default=(), max_length=2)
    dependency_request: DependencyRequestV1 | None = None

    @model_validator(mode="after")
    def binding(self) -> Self:
        if not self.native_requests and self.dependency_request is None:
            raise ValueError("publication requires a selected operation")
        if len({r.profile.tool for r in self.native_requests}) != len(self.native_requests):
            raise ValueError("duplicate native operation")
        requests: tuple[ScannerRequestV1 | DependencyRequestV1, ...] = self.native_requests
        if self.dependency_request is not None:
            requests += (self.dependency_request,)
            if self.dependency_request.runtime_digest != self.runtime_digest:
                raise ValueError("dependency runtime differs")
            if self.dependency_request.evaluated_at != self.captured_at:
                raise ValueError("dependency evaluation time differs")
        files: dict[UUID, tuple[str, int]] = {}
        for request in requests:
            if (request.scope, request.repository_id, request.config_hash) != (
                self.scope,
                self.repository_id,
                self.config_hash,
            ):
                raise ValueError("publication request scope differs")
            for file in request.files:
                identity = (file.sha256, file.size_bytes)
                if file.file_id in files and files[file.file_id] != identity:
                    raise ValueError("conflicting selected source metadata")
                files[file.file_id] = identity
        return self


class RecommendationV1(Contract):
    action: str = Field(min_length=1, max_length=1024)
    benefit: str = Field(min_length=1, max_length=512)
    trade_off: str = Field(min_length=1, max_length=512)
    effort: Literal["small", "moderate", "unknown"]
    source: RuleId


class NativeFindingEntityV1(Contract):
    kind: Literal["source"] = "source"
    tool: Literal["semgrep", "gitleaks"]
    file_id: UUID
    sha256: Digest
    range: SourceRangeV1


class DependencyFindingEntityV1(Contract):
    kind: Literal["dependency"] = "dependency"
    file_id: UUID
    sha256: Digest
    location: str = Field(min_length=1, max_length=1024)
    query: PackageQuery
    advisory_id: RuleId
    record_digest: Digest


FindingEntity = Annotated[
    NativeFindingEntityV1 | DependencyFindingEntityV1, Field(discriminator="kind")
]


class FindingOccurrenceV1(Contract):
    schema_version: Literal[1] = 1
    scope: RunRef
    id: UUID
    category: Category
    rule_id: RuleId
    policy_id: RuleId
    policy_version: RuleId
    title: str = Field(min_length=1, max_length=256)
    severity: Literal["info", "low", "medium", "high", "critical"]
    severity_basis: str = Field(min_length=1, max_length=512)
    entity: FindingEntity
    observation_digest: Digest
    support: tuple[EvidenceRef, ...] = Field(min_length=1, max_length=8)
    recommendation: RecommendationV1
    limitations: tuple[str, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def scoped(self) -> Self:
        if any(ref.scope != self.scope for ref in self.support):
            raise ValueError("foreign finding support")
        if len({ref.evidence_id for ref in self.support}) != len(self.support):
            raise ValueError("duplicate finding support")
        if (self.category == "dependency_advisory") != (self.entity.kind == "dependency"):
            raise ValueError("finding category differs from affected entity")
        if self.entity.kind == "source" and (
            (self.category == "secret_format") != (self.entity.tool == "gitleaks")
        ):
            raise ValueError("finding category differs from scanner")
        if any(not text or len(text.encode()) > 1024 for text in self.limitations):
            raise ValueError("finding limitations exceed budget")
        return self


class VerificationDecisionV1(Contract):
    scope: RunRef
    finding_id: UUID
    disposition: Literal["proposed", "supported-static", "contradicted", "unresolved"]
    obligations: tuple[Obligation, ...] = Field(min_length=1, max_length=5)
    support: tuple[EvidenceRef, ...] = Field(min_length=1, max_length=8)


class FindingSupportPayloadV1(Contract):
    category: Category
    request_digest: Digest
    result_digest: Digest
    observation_digest: Digest
    policy_id: RuleId
    policy_version: RuleId


class NativePublicationResultV1(Contract):
    tool: Literal["semgrep", "gitleaks"]
    result: ScannerResultV1


class ScanPublicationDraftV1(Contract):
    schema_version: Literal[1] = 1
    spec: ScanPublicationSpecV1
    coverage: Literal["complete", "partial", "unknown"]
    native_results: tuple[NativePublicationResultV1, ...] = Field(max_length=2)
    dependency_result: DependencyResultV1 | None = None
    findings: tuple[FindingOccurrenceV1, ...] = Field(max_length=1000)
    evidence: tuple[EvidenceEnvelopeV1, ...] = Field(max_length=1000)
    decisions: tuple[VerificationDecisionV1, ...] = Field(max_length=1000)
    omitted_findings: int = Field(default=0, ge=0, le=502000)
    omission_reason: Literal["finding_limit"] | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if len(canonical_bytes(self.model_dump())) > 4194304:
            raise ValueError("publication exceeds aggregate byte budget")
        if bool(self.omitted_findings) != (self.omission_reason is not None):
            raise ValueError("finding omission accounting differs")
        expected = {r.profile.tool: r for r in self.spec.native_requests}
        if (
            len({r.tool for r in self.native_results}) != len(self.native_results)
            or {r.tool for r in self.native_results} != expected.keys()
        ):
            raise ValueError("native result set differs")
        complete, observed = True, False
        digests: dict[str, tuple[str, str]] = {}
        for item in self.native_results:
            item.result.validate_for(expected[item.tool])
            digests[item.tool] = (
                expected[item.tool].semantic_hash(),
                semantic_digest(item.result.model_dump()),
            )
            complete &= item.result.status == "completed" and bool(expected[item.tool].files)
            observed |= bool(item.result.processed_file_ids)
        if (self.dependency_result is None) != (self.spec.dependency_request is None):
            raise ValueError("dependency result set differs")
        if self.dependency_result is not None:
            assert self.spec.dependency_request is not None
            self.dependency_result.validate_for(self.spec.dependency_request)
            digests["dependencies"] = (
                self.spec.dependency_request.semantic_hash(),
                semantic_digest(self.dependency_result.model_dump()),
            )
            complete &= self.dependency_result.coverage == "complete"
            observed |= any(f.status == "parsed" for f in self.dependency_result.files)
        candidate_count = sum(len(r.result.observations) for r in self.native_results)
        if self.dependency_result is not None:
            candidate_count += sum(
                len(p.assessment.advisories) for p in self.dependency_result.packages
            )
        if len(self.findings) != min(
            candidate_count, 1000
        ) or self.omitted_findings != candidate_count - len(self.findings):
            raise ValueError("finding candidate accounting differs")
        coverage = "complete" if complete else "partial" if observed else "unknown"
        if self.omitted_findings and coverage == "complete":
            coverage = "partial"
        if self.coverage != coverage:
            raise ValueError("publication coverage differs")
        scope = self.spec.scope
        findings = {f.id: f for f in self.findings}
        evidence = {e.id: e for e in self.evidence}
        decisions = {d.finding_id: d for d in self.decisions}
        if (
            len(findings) != len(self.findings)
            or len(evidence) != len(self.evidence)
            or len(decisions) != len(self.decisions)
            or findings.keys() != decisions.keys()
        ):
            raise ValueError("duplicate or incomplete publication set")
        if len(
            {
                (
                    f.category,
                    f.rule_id,
                    canonical_bytes(f.entity.model_dump()),
                    f.observation_digest,
                )
                for f in self.findings
            }
        ) != len(self.findings):
            raise ValueError("duplicate affected finding identity")
        referenced: set[UUID] = set()
        for finding in self.findings:
            decision = decisions[finding.id]
            if (
                finding.scope != scope
                or decision.scope != scope
                or decision.support != finding.support
            ):
                raise ValueError("foreign verification scope or support")
            if decision.obligations != required_obligations(finding.category):
                raise ValueError("verification obligations differ from category")
            for ref in finding.support:
                record = evidence.get(ref.evidence_id)
                if (
                    record is None
                    or record.scope != scope
                    or record.file_id != finding.entity.file_id
                ):
                    raise ValueError("missing or foreign evidence")
                payload = FindingSupportPayloadV1.model_validate(record.payload)
                if (
                    payload.category,
                    payload.observation_digest,
                    payload.policy_id,
                    payload.policy_version,
                ) != (
                    finding.category,
                    finding.observation_digest,
                    finding.policy_id,
                    finding.policy_version,
                ):
                    raise ValueError("evidence policy differs")
                self._check_support(finding, record, payload, digests)
                referenced.add(record.id)
        if referenced != evidence.keys():
            raise ValueError("orphan publication evidence")
        return self

    def _check_support(
        self,
        finding: FindingOccurrenceV1,
        record: EvidenceEnvelopeV1,
        payload: FindingSupportPayloadV1,
        digests: dict[str, tuple[str, str]],
    ) -> None:
        if (
            record.captured_at != self.spec.captured_at
            or record.operation != finding.rule_id
            or record.coverage_status != "complete"
        ):
            raise ValueError("evidence provenance differs")
        entity = finding.entity
        if isinstance(entity, NativeFindingEntityV1):
            item = next((r for r in self.native_results if r.tool == entity.tool), None)
            if item is None:
                raise ValueError("source evidence tool unavailable")
            observation = next(
                (
                    o
                    for o in item.result.observations
                    if (
                        o.rule_id == finding.rule_id
                        and o.file_id == entity.file_id
                        and o.sha256 == entity.sha256
                        and o.range == entity.range
                    )
                ),
                None,
            )
            if (
                observation is None
                or semantic_digest(observation.model_dump()) != finding.observation_digest
                or record.range != entity.range
                or record.provider_name != entity.tool
                or record.observation_mode != "static"
                or record.basis != "observed"
                or (payload.request_digest, payload.result_digest) != digests[entity.tool]
            ):
                raise ValueError("source evidence differs from observation")
        else:
            result, dependency = self.dependency_result, self.spec.dependency_request
            if result is None or dependency is None:
                raise ValueError("dependency evidence unavailable")
            package = next(
                (
                    p
                    for p in result.packages
                    if (
                        p.file_id == entity.file_id
                        and p.location == entity.location
                        and p.query == entity.query
                    )
                ),
                None,
            )
            file = next((f for f in dependency.files if f.file_id == entity.file_id), None)
            if (
                package is None
                or file is None
                or file.sha256 != entity.sha256
                or package.assessment.status != "matched"
                or (entity.advisory_id, entity.record_digest) not in package.assessment.advisories
                or finding.rule_id != entity.advisory_id
                or record.range is not None
                or record.provider_name != "offline-osv"
                or record.observation_mode != "external"
                or record.basis != "derived"
                or (payload.request_digest, payload.result_digest) != digests["dependencies"]
            ):
                raise ValueError("dependency evidence differs from instance")
            if finding.observation_digest != semantic_digest(
                {
                    "package": package.model_dump(),
                    "advisory": entity.advisory_id,
                    "record_digest": entity.record_digest,
                }
            ):
                raise ValueError("dependency observation identity differs")
