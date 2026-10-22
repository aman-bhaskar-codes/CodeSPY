"""Pure scoped projections of a complete safe scan draft."""

from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime

from app.contracts.findings import (
    FindingOccurrenceV1,
    ScanPublicationDraftV1,
    VerificationDecisionV1,
)
from app.contracts.persistence import CoverageV1, ResultBatchV1
from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.domain.scope import Contract


class ObservationProjectionV1(Contract):
    origin: Literal["semgrep", "gitleaks", "dependencies"]
    kind: Literal["native", "dependency_instance", "unresolved_dependency"]
    file_id: UUID
    observation_key: str
    payload_json: dict[str, Any]
    canonical_text: str
    payload_digest: str


class FindingProjectionV1(Contract):
    occurrence: FindingOccurrenceV1
    observation_key: str


class ScanProjectionsV1(Contract):
    observations: tuple[ObservationProjectionV1, ...]
    findings: tuple[FindingProjectionV1, ...]
    decisions: tuple[VerificationDecisionV1, ...]
    support: tuple[tuple[UUID, UUID], ...]
    batch: ResultBatchV1
    draft_digest: str
    batch_hash: str


def project_scan(draft: ScanPublicationDraftV1) -> ScanProjectionsV1:
    draft = ScanPublicationDraftV1.model_validate(draft.model_dump())
    observations: dict[str, ObservationProjectionV1] = {}
    native_keys: dict[tuple[str, str], str] = {}
    package_keys: dict[tuple[UUID, str], str] = {}

    def record(
        origin: Literal["semgrep", "gitleaks", "dependencies"],
        kind: Literal["native", "dependency_instance", "unresolved_dependency"],
        file_id: UUID,
        payload: dict[str, Any],
    ) -> str:
        raw = canonical_bytes(payload)
        digest = semantic_digest(payload)
        key = origin + ":" + digest
        if key in observations:
            raise ValueError("duplicate scan observation")
        observations[key] = ObservationProjectionV1(
            origin=origin,
            kind=kind,
            file_id=file_id,
            observation_key=key,
            payload_json=payload,
            canonical_text=raw.decode(),
            payload_digest=digest,
        )
        return key

    for native in draft.native_results:
        for observation in native.result.observations:
            payload = observation.model_dump(mode="json")
            key = record(native.tool, "native", observation.file_id, payload)
            native_keys[native.tool, semantic_digest(payload)] = key
    dependency = draft.dependency_result
    if dependency is not None:
        for package in dependency.packages:
            package_keys[package.file_id, package.location] = record(
                "dependencies",
                "dependency_instance",
                package.file_id,
                package.model_dump(mode="json"),
            )
        for unresolved in dependency.unresolved:
            record(
                "dependencies",
                "unresolved_dependency",
                unresolved.file_id,
                unresolved.model_dump(mode="json"),
            )
    findings = []
    for finding in draft.findings:
        entity = finding.entity
        key = (
            native_keys[entity.tool, finding.observation_digest]
            if entity.kind == "source"
            else package_keys[entity.file_id, entity.location]
        )
        findings.append(FindingProjectionV1(occurrence=finding, observation_key=key))
    units = len(draft.native_results) + (dependency is not None)
    processed = sum(n.result.status == "completed" for n in draft.native_results)
    processed += dependency is not None and dependency.coverage == "complete"
    batch = ResultBatchV1(
        scope=draft.spec.scope,
        evidence=list(draft.evidence),
        disposition="completed" if draft.coverage == "complete" else "partial",
        coverage=CoverageV1(
            status=draft.coverage,
            scope_description="Selected offline scan operations, not source files",
            admitted_count=units,
            processed_count=processed,
            omitted_count=units - processed,
            truncated=bool(draft.omitted_findings),
        ),
        limitations=["Static findings do not establish execution or exploitability."],
    )
    return ScanProjectionsV1(
        observations=tuple(observations[k] for k in sorted(observations)),
        findings=tuple(sorted(findings, key=lambda f: str(f.occurrence.id))),
        decisions=draft.decisions,
        support=tuple(
            sorted(
                ((f.id, r.evidence_id) for f in draft.findings for r in f.support),
                key=lambda pair: (str(pair[0]), str(pair[1])),
            )
        ),
        batch=batch,
        draft_digest=semantic_digest(draft.model_dump()),
        batch_hash=batch.semantic_hash(),
    )


def stored_draft(value: Any) -> ScanPublicationDraftV1:
    return ScanPublicationDraftV1.model_validate_json(canonical_bytes(value))


SupportReason = Literal[
    "current_support", "source_unavailable", "bundle_unavailable", "advisory_not_current"
]


class EffectiveVerificationV1(Contract):
    finding_id: UUID
    historical_disposition: Literal["proposed", "supported-static", "contradicted", "unresolved"]
    effective_disposition: Literal["proposed", "supported-static", "contradicted", "unresolved"]
    reason: SupportReason


class ScanReadResultV1(Contract):
    historical: ScanPublicationDraftV1
    checked_at: AwareDatetime
    effective_decisions: tuple[EffectiveVerificationV1, ...]
