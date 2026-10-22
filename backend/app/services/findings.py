"""Materialize safe findings from completed broker operations; no persistence I/O."""

from typing import Literal
from uuid import UUID

from app.contracts.dependencies import DependencyRequestV1, DependencyResultV1
from app.contracts.evidence import EvidenceEnvelopeV1, EvidenceRef
from app.contracts.findings import (
    DependencyFindingEntityV1,
    FindingEntity,
    FindingOccurrenceV1,
    FindingSupportPayloadV1,
    NativeFindingEntityV1,
    NativePublicationResultV1,
    RecommendationV1,
    ScanPublicationDraftV1,
    VerificationDecisionV1,
    required_obligations,
)
from app.contracts.findings import (
    ScanPublicationSpecV1 as ScanPublicationSpecV1,
)
from app.contracts.scanners import ScannerRequestV1, ScannerResultV1
from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.scanners.advisories import AdvisoryBundle
from app.scanners.finding_catalog import (
    FindingCatalogV1,
    FindingPolicyV1,
)
from app.scanners.finding_catalog import (
    reviewed_catalog as reviewed_catalog,
)
from app.scanners.receipts import BrokerReceipt


class FindingMaterializer:
    def __init__(self, catalog: FindingCatalogV1):
        self.catalog = FindingCatalogV1.model_validate(catalog.model_dump())

    def materialize(
        self,
        spec: ScanPublicationSpecV1,
        native: tuple[BrokerReceipt[ScannerRequestV1, ScannerResultV1], ...],
        *,
        dependency: BrokerReceipt[DependencyRequestV1, DependencyResultV1] | None = None,
        bundle: AdvisoryBundle | None = None,
    ) -> ScanPublicationDraftV1:
        spec = ScanPublicationSpecV1.model_validate(spec.model_dump())
        if spec.catalog_digest != self.catalog.semantic_hash():
            raise ValueError("finding catalog differs")
        expected = {r.profile.tool: r for r in spec.native_requests}
        received: dict[str, BrokerReceipt[ScannerRequestV1, ScannerResultV1]] = {}
        for receipt in native:
            if not isinstance(receipt, BrokerReceipt) or not isinstance(
                receipt.request, ScannerRequestV1
            ):
                raise ValueError("native broker receipt required")
            receipt.validate()
            request = receipt.request
            if (
                request.profile.tool in received
                or request != expected.get(request.profile.tool)
                or receipt.image != "sha256:" + spec.runtime_digest
            ):
                raise ValueError("native broker binding differs")
            received[request.profile.tool] = receipt
        if received.keys() != expected.keys():
            raise ValueError("incomplete native broker set")
        if (dependency is None) != (spec.dependency_request is None):
            raise ValueError("incomplete dependency broker set")
        if dependency is not None:
            if not isinstance(dependency, BrokerReceipt) or not isinstance(
                dependency.request, DependencyRequestV1
            ):
                raise ValueError("dependency broker receipt required")
            dependency.validate()
            if (
                dependency.request != spec.dependency_request
                or dependency.image != "sha256:" + spec.runtime_digest
                or dependency.provider_version != dependency.request.parser_profile
                or bundle is None
                or bundle.digest() != dependency.request.bundle_digest
            ):
                raise ValueError("dependency broker binding differs")
            bundle = AdvisoryBundle.model_validate(bundle.model_dump())
            for package in dependency.result.packages:
                if (
                    bundle.evaluate(package.query, now=dependency.request.evaluated_at)
                    != package.assessment
                ):
                    raise ValueError("dependency advisory evidence differs")
        elif bundle is not None:
            raise ValueError("unexpected dependency evidence")

        findings: list[FindingOccurrenceV1] = []
        evidence: list[EvidenceEnvelopeV1] = []
        decisions: list[VerificationDecisionV1] = []
        count = 0
        spec_digest = semantic_digest(spec.model_dump())

        def add(
            policy: FindingPolicyV1,
            entity: FindingEntity,
            observation_digest: str,
            request_digest: str,
            result_digest: str,
            provider: str,
        ) -> None:
            nonlocal count
            count += 1
            if len(findings) >= 1000:
                return
            identity = semantic_digest(
                {
                    "spec": spec_digest,
                    "rule": policy.rule_id,
                    "entity": entity.model_dump(),
                    "observation": observation_digest,
                }
            )
            finding_id = UUID(hex=identity[:32])
            evidence_id = UUID(
                hex=semantic_digest({"finding": str(finding_id), "kind": "support"})[:32]
            )
            ref = EvidenceRef(scope=spec.scope, evidence_id=evidence_id)
            payload = FindingSupportPayloadV1(
                category=policy.category,
                request_digest=request_digest,
                result_digest=result_digest,
                observation_digest=observation_digest,
                policy_id=self.catalog.policy_id,
                policy_version=self.catalog.policy_version,
            )
            evidence.append(
                EvidenceEnvelopeV1(
                    scope=spec.scope,
                    id=evidence_id,
                    file_id=entity.file_id,
                    range=entity.range if entity.kind == "source" else None,
                    provider_name=provider,
                    provider_version=policy.provider_version,
                    operation=policy.rule_id,
                    observation_mode="static" if entity.kind == "source" else "external",
                    basis="observed" if entity.kind == "source" else "derived",
                    coverage_status="complete",
                    payload=payload.model_dump(),
                    limitations=list(policy.limitations),
                    captured_at=spec.captured_at,
                )
            )
            findings.append(
                FindingOccurrenceV1(
                    scope=spec.scope,
                    id=finding_id,
                    category=policy.category,
                    rule_id=policy.rule_id,
                    policy_id=self.catalog.policy_id,
                    policy_version=self.catalog.policy_version,
                    title=policy.title,
                    severity=policy.severity,
                    severity_basis=policy.severity_basis,
                    entity=entity,
                    observation_digest=observation_digest,
                    support=(ref,),
                    recommendation=policy.recommendation,
                    limitations=policy.limitations,
                )
            )
            decisions.append(
                VerificationDecisionV1(
                    scope=spec.scope,
                    finding_id=finding_id,
                    disposition="supported-static",
                    obligations=required_obligations(policy.category),
                    support=(ref,),
                )
            )

        for tool, receipt in sorted(received.items()):
            profile_digest = semantic_digest(receipt.request.profile.model_dump())
            policies = {
                p.rule_id: p for p in self.catalog.native if p.profile_digest == profile_digest
            }
            if set(policies) != set(receipt.request.profile.rule_ids) or any(
                p.provider_version != receipt.provider_version for p in policies.values()
            ):
                raise ValueError("native predicate policy differs")
            for observation in sorted(
                receipt.result.observations, key=lambda o: canonical_bytes(o.model_dump())
            ):
                add(
                    policies[observation.rule_id],
                    NativeFindingEntityV1(
                        tool=receipt.request.profile.tool,
                        file_id=observation.file_id,
                        sha256=observation.sha256,
                        range=observation.range,
                    ),
                    semantic_digest(observation.model_dump()),
                    receipt.request.semantic_hash(),
                    receipt.result_digest,
                    tool,
                )

        if dependency is not None:
            files = {f.file_id: f for f in dependency.request.files}
            for package in sorted(
                dependency.result.packages, key=lambda p: (str(p.file_id), p.location)
            ):
                for advisory_id, digest in sorted(package.assessment.advisories):
                    policy = FindingPolicyV1(
                        rule_id=advisory_id,
                        category="dependency_advisory",
                        profile_digest=dependency.request.bundle_digest,
                        provider_version=dependency.request.parser_profile,
                        title="Dependency advisory match",
                        severity="info",
                        severity_basis="Exact offline advisory membership; severity is unassessed.",
                        recommendation=RecommendationV1(
                            action="Review the advisory and a compatible dependency update.",
                            benefit="Address a reported dependency issue.",
                            trade_off="Updates may change compatibility.",
                            effort="unknown",
                            source="offline-advisory-review",
                        ),
                        limitations=(
                            "Exact selected lock instance; installation, environment "
                            "and reachability are unproven.",
                        ),
                    )
                    entity = DependencyFindingEntityV1(
                        file_id=package.file_id,
                        sha256=files[package.file_id].sha256,
                        location=package.location,
                        query=package.query,
                        advisory_id=advisory_id,
                        record_digest=digest,
                    )
                    add(
                        policy,
                        entity,
                        semantic_digest(
                            {
                                "package": package.model_dump(),
                                "advisory": advisory_id,
                                "record_digest": digest,
                            }
                        ),
                        dependency.request.semantic_hash(),
                        dependency.result_digest,
                        "offline-osv",
                    )

        complete = all(
            r.result.status == "completed" and bool(r.request.files) for r in received.values()
        )
        observed = any(r.result.processed_file_ids for r in received.values())
        if dependency is not None:
            complete &= dependency.result.coverage == "complete"
            observed |= any(f.status == "parsed" for f in dependency.result.files)
        omitted = count - len(findings)
        coverage: Literal["complete", "partial", "unknown"] = (
            "complete" if complete and not omitted else "partial" if observed else "unknown"
        )
        return ScanPublicationDraftV1(
            spec=spec,
            coverage=coverage,
            native_results=tuple(
                NativePublicationResultV1(tool=r.request.profile.tool, result=r.result)
                for _, r in sorted(received.items())
            ),
            dependency_result=dependency.result if dependency is not None else None,
            findings=tuple(findings),
            evidence=tuple(evidence),
            decisions=tuple(decisions),
            omitted_findings=omitted,
            omission_reason="finding_limit" if omitted else None,
        )
