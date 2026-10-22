"""Synthetic broker inputs test publication semantics, not native qualification."""

import importlib
from datetime import timedelta
from uuid import UUID

import pytest
from app.analyzers.positions import SourceIndex
from app.contracts.scanners import ScannerObservationV1, ScannerResultV1
from app.domain.canonical_json import canonical_bytes
from app.scanners.advisories import AdvisoryBundle, AdvisoryRecord, ExactAnswer, PackageQuery
from app.scanners.dependency_worker import DependencyInputV1, assess_dependencies
from app.scanners.receipts import _issue
from tests.phase4.dependency_fixtures import NOW, fixture_dependency
from tests.phase4.unit.test_scanner_adapter import IMAGE, make_adapter


def api():
    assert importlib.util.find_spec("app.services.findings") is not None, (
        "findings materializer is missing"
    )
    return importlib.import_module("app.services.findings")


def native_case(tmp_path):
    module = api()
    _, _, _, request = make_adapter(tmp_path)
    span = SourceIndex(b"def hello():\n    return 'world'\n").range_from_bytes(0, 11)
    result = ScannerResultV1(
        request_digest=request.semantic_hash(),
        status="completed",
        processed_file_ids=(request.files[0].file_id,),
        omitted_file_ids=(),
        observations=(
            ScannerObservationV1(
                rule_id="python.shell-true",
                file_id=request.files[0].file_id,
                sha256=request.files[0].sha256,
                range=span,
            ),
        ),
    )
    # Unit-only trusted broker seam; this fixture does not run a native predicate.
    receipt = _issue(request, result, IMAGE, "1.179.0")
    catalog = module.reviewed_catalog((request.profile,))
    spec = module.ScanPublicationSpecV1(
        scope=request.scope,
        repository_id=request.repository_id,
        config_hash=request.config_hash,
        runtime_digest="a" * 64,
        catalog_digest=catalog.semantic_hash(),
        captured_at=NOW,
        native_requests=(request,),
    )
    return module, catalog, spec, receipt


def test_native_occurrence_has_scoped_support_and_no_execution_claim(tmp_path):
    module, catalog, spec, receipt = native_case(tmp_path)
    draft = module.FindingMaterializer(catalog).materialize(spec, (receipt,))
    finding = draft.findings[0]
    assert finding.title == "Explicit shell execution"
    assert finding.category == "source_pattern"
    assert draft.decisions[0].disposition == "supported-static"
    assert set(draft.decisions[0].obligations) == {"authenticated-source", "qualified-predicate"}
    assert finding.support[0].scope == spec.scope
    assert finding.support[0].evidence_id == draft.evidence[0].id
    assert finding.entity.file_id == receipt.request.files[0].file_id
    assert draft.coverage == "complete"
    assert draft == module.FindingMaterializer(catalog).materialize(spec, (receipt,))


@pytest.mark.parametrize("change", ["run", "image", "catalog", "raw", "missing", "version"])
def test_foreign_or_unattested_results_cannot_materialize(tmp_path, change):
    module, catalog, spec, receipt = native_case(tmp_path)
    values = (receipt,)
    if change == "run":
        spec = spec.model_copy(
            update={"scope": spec.scope.model_copy(update={"run_id": UUID(int=999)})}
        )
    elif change == "image":
        spec = spec.model_copy(update={"runtime_digest": "f" * 64})
    elif change == "catalog":
        spec = spec.model_copy(update={"catalog_digest": "f" * 64})
    elif change == "raw":
        values = (receipt.result,)
    elif change == "missing":
        values = ()
    else:
        values = (_issue(receipt.request, receipt.result, IMAGE, "9.9.9"),)
    with pytest.raises(ValueError):
        module.FindingMaterializer(catalog).materialize(spec, values)


def matched_bundle(*, status="matched"):
    query = PackageQuery(ecosystem="npm", name="example", version="1.2.3")
    record = AdvisoryRecord(
        id="TEST-2026-1",
        source="https://api.osv.dev/v1/vulns/TEST-2026-1",
        modified=NOW,
        document={
            "id": "TEST-2026-1",
            "modified": NOW.isoformat(),
            "affected": [{"package": {"ecosystem": "npm", "name": "example"}}],
            "summary": "private-content-canary",
            **({"withdrawn": NOW.isoformat()} if status == "withdrawn" else {}),
        },
    )
    return AdvisoryBundle(
        answers=()
        if status == "missing"
        else (
            ExactAnswer(
                query=query,
                fetched_at=NOW - timedelta(days=8) if status == "stale" else NOW,
                complete=status != "incomplete",
                advisory_ids=() if status == "empty" else (record.id,),
            ),
        ),
        records=(record,),
    )


def dependency_case(tmp_path, status="matched", *, custom_bundle=None):
    module = api()
    source, bundle = fixture_dependency(
        tmp_path, bundle=custom_bundle or matched_bundle(status=status)
    )
    result = assess_dependencies(DependencyInputV1(source=source, bundle=bundle), tmp_path)
    catalog = module.reviewed_catalog(())
    spec = module.ScanPublicationSpecV1(
        scope=source.request.scope,
        repository_id=source.request.repository_id,
        config_hash=source.request.config_hash,
        runtime_digest=source.request.runtime_digest,
        catalog_digest=catalog.semantic_hash(),
        captured_at=NOW,
        dependency_request=source.request,
    )
    receipt = _issue(source.request, result, IMAGE, source.request.parser_profile)
    return module, catalog, spec, receipt, bundle


@pytest.mark.parametrize(
    "status", ["matched", "empty", "missing", "stale", "incomplete", "withdrawn"]
)
def test_admitted_lock_to_findings_preserves_evidence_and_unknown_coverage(tmp_path, status):
    module, catalog, spec, receipt, bundle = dependency_case(tmp_path, status)
    draft = module.FindingMaterializer(catalog).materialize(
        spec, (), dependency=receipt, bundle=bundle
    )
    assert len(draft.findings) == (1 if status == "matched" else 0)
    assert draft.coverage == ("complete" if status in {"matched", "empty"} else "partial")
    assert b"private-content-canary" not in canonical_bytes(draft.model_dump())
    if status == "matched":
        assert draft.findings[0].severity == "info"
        assert draft.findings[0].entity.location == "node_modules/example"
        assert draft.findings[0].entity.advisory_id == "TEST-2026-1"
        assert draft.decisions[0].obligations == ("exact-lock-query", "fresh-complete-advisory")
        assert "reachability" in " ".join(draft.findings[0].limitations)


def test_dependency_assessment_is_recomputed_not_accepted_from_receipt(tmp_path):
    module, catalog, spec, receipt, bundle = dependency_case(tmp_path)
    package = receipt.result.packages[0]
    from app.contracts.dependencies import AdvisoryAssessment

    result = receipt.result.model_copy(
        update={
            "packages": (
                package.model_copy(update={"assessment": AdvisoryAssessment(status="empty")}),
            )
        }
    )
    altered = _issue(receipt.request, result, IMAGE, receipt.provider_version)
    with pytest.raises(ValueError):
        module.FindingMaterializer(catalog).materialize(spec, (), dependency=altered, bundle=bundle)


def test_publication_rejects_dangling_support_and_empty_verification_obligations(tmp_path):
    module, catalog, spec, receipt = native_case(tmp_path)
    draft = module.FindingMaterializer(catalog).materialize(spec, (receipt,))
    changed = draft.model_dump()
    changed["evidence"] = ()
    with pytest.raises(ValueError):
        type(draft).model_validate(changed)
    changed = draft.model_dump()
    changed["decisions"][0]["obligations"] = ()
    with pytest.raises(ValueError):
        type(draft).model_validate(changed)


def test_publication_rejects_unsupported_runtime_verification(tmp_path):
    module, catalog, spec, receipt = native_case(tmp_path)
    draft = module.FindingMaterializer(catalog).materialize(spec, (receipt,))
    changed = draft.model_dump()
    changed["decisions"][0]["disposition"] = "verified-scenario"
    with pytest.raises(ValueError):
        type(draft).model_validate(changed)


@pytest.mark.parametrize("change", ["digest", "range", "provider", "file", "time"])
def test_source_evidence_must_describe_the_actual_affected_observation(tmp_path, change):
    module, catalog, spec, receipt = native_case(tmp_path)
    draft = module.FindingMaterializer(catalog).materialize(spec, (receipt,))
    changed = draft.model_dump()
    record = changed["evidence"][0]
    if change == "digest":
        record["payload"]["result_digest"] = "f" * 64
    elif change == "range":
        record["range"] = (
            SourceIndex(b"def hello():\n    return 'world'\n").range_from_bytes(1, 11).model_dump()
        )
    elif change == "provider":
        record["provider_name"] = "other-tool"
    elif change == "file":
        changed["findings"][0]["entity"]["sha256"] = "f" * 64
    else:
        record["captured_at"] = NOW - timedelta(days=1)
    with pytest.raises(ValueError):
        type(draft).model_validate(changed)


def test_secret_normalization_to_finding_never_retains_token_or_raw_diagnostics():
    import hashlib

    from app.contracts.scanners import ScannerRequestV1
    from tests.phase4.unit.test_gitleaks_normalization import FILE, TOKEN, scan

    module = api()
    result = scan(TOKEN)
    request = ScannerRequestV1.model_validate(
        {
            "scope": {"org_id": UUID(int=2), "snapshot_id": UUID(int=3), "run_id": UUID(int=4)},
            "repository_id": UUID(int=5),
            "config_hash": "a" * 64,
            "profile": {
                "tool": "gitleaks",
                "artifact_sha256": "b" * 64,
                "rules_sha256": "c" * 64,
                "rule_ids": ("secret.github-classic-pat",),
            },
            "files": (
                {"file_id": FILE, "sha256": hashlib.sha256(TOKEN).hexdigest(), "size_bytes": 40},
            ),
        }
    )
    catalog = module.reviewed_catalog((request.profile,))
    spec = module.ScanPublicationSpecV1(
        scope=request.scope,
        repository_id=request.repository_id,
        config_hash=request.config_hash,
        runtime_digest="a" * 64,
        catalog_digest=catalog.semantic_hash(),
        captured_at=NOW,
        native_requests=(request,),
    )
    draft = module.FindingMaterializer(catalog).materialize(
        spec, (_issue(request, result, IMAGE, "8.30.1"),)
    )
    output = canonical_bytes(draft.model_dump())
    assert TOKEN not in output
    assert b"PRIVATE-CANARY" not in output
    assert draft.findings[0].category == "secret_format"
    assert draft.decisions[0].obligations == ("authenticated-source", "selected-format")
    assert "unverified" in " ".join(draft.findings[0].limitations)


def test_finding_limit_preserves_instances_and_reports_omissions(tmp_path):
    from app.contracts.dependencies import DependencyPackageV1

    module, catalog, spec, receipt, bundle = dependency_case(tmp_path)
    original = receipt.result.packages[0]
    packages = tuple(
        DependencyPackageV1(
            **{**original.model_dump(), "location": f"node_modules/p{i:04d}/node_modules/example"}
        )
        for i in range(1001)
    )
    result = receipt.result.model_copy(update={"packages": packages})
    first = module.FindingMaterializer(catalog).materialize(
        spec,
        (),
        dependency=_issue(receipt.request, result, IMAGE, receipt.provider_version),
        bundle=bundle,
    )
    second = module.FindingMaterializer(catalog).materialize(
        spec,
        (),
        dependency=_issue(
            receipt.request,
            result.model_copy(update={"packages": packages[::-1]}),
            IMAGE,
            receipt.provider_version,
        ),
        bundle=bundle,
    )
    assert len(first.findings) == len(first.evidence) == len(first.decisions) == 1000
    assert first.omitted_findings == 1
    assert first.omission_reason == "finding_limit"
    assert first.coverage == "partial"
    assert len({f.entity.location for f in first.findings}) == 1000
    # Reordered transport bytes change result provenance but not affected-instance selection.
    assert [f.entity for f in first.findings] == [f.entity for f in second.findings]


def test_aggregate_overflow_rejects_complete_draft_instead_of_truncating_support(tmp_path):
    module, catalog, spec, receipt = native_case(tmp_path)
    draft = module.FindingMaterializer(catalog).materialize(spec, (receipt,))
    changed = draft.model_dump()
    record = changed["evidence"][0]
    # Duplicate-free valid bounded findings force the aggregate bound, not a per-record bound.
    finding = changed["findings"][0]
    decision = changed["decisions"][0]
    evidence, findings, decisions = [], [], []
    for i in range(1000):
        identity = UUID(int=i + 1000)
        ref = {"scope": spec.scope.model_dump(), "evidence_id": identity}
        evidence.append({**record, "id": identity, "limitations": ["a" * 2048] * 3})
        findings.append({**finding, "id": identity, "support": (ref,)})
        decisions.append({**decision, "finding_id": identity, "support": (ref,)})
    changed.update(evidence=tuple(evidence), findings=tuple(findings), decisions=tuple(decisions))
    with pytest.raises(ValueError, match="aggregate byte budget"):
        type(draft).model_validate(changed)


@pytest.mark.parametrize("change", ["missing", "invented_omissions", "foreign_tool"])
def test_publication_requires_complete_accounting_of_candidate_findings(tmp_path, change):
    module, catalog, spec, receipt = native_case(tmp_path)
    draft = module.FindingMaterializer(catalog).materialize(spec, (receipt,))
    changed = draft.model_dump()
    if change == "missing":
        changed.update(findings=(), evidence=(), decisions=())
    elif change == "invented_omissions":
        changed.update(omitted_findings=1, omission_reason="finding_limit", coverage="partial")
    else:
        changed["findings"][0]["entity"]["tool"] = "gitleaks"
        changed["findings"][0]["category"] = "secret_format"
        changed["evidence"][0]["payload"]["category"] = "secret_format"
        changed["decisions"][0]["obligations"] = ("authenticated-source", "selected-format")
    with pytest.raises(ValueError):
        type(draft).model_validate(changed)
