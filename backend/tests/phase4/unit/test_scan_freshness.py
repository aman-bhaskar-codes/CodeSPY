"""Pure current-support evaluation uses fetched-at age, not publication age."""

from datetime import timedelta

import pytest
from app.services.findings import FindingMaterializer

from tests.phase4.dependency_fixtures import NOW
from tests.phase4.unit.test_findings import dependency_case, matched_bundle


def test_current_dependency_support_requires_retained_bundle_registry(tmp_path):
    from app.services.scan_reads import effective_decisions

    _, catalog, spec, receipt, bundle = dependency_case(tmp_path)
    draft = FindingMaterializer(catalog).materialize(spec, (), dependency=receipt, bundle=bundle)
    decision = effective_decisions(draft, True, None, NOW)[0]
    assert decision.historical_disposition == "supported-static"
    assert decision.effective_disposition == "unresolved"
    assert decision.reason == "bundle_unavailable"


def test_expiry_can_precede_seven_days_after_publication(tmp_path):
    from app.services import scan_reads

    assert hasattr(scan_reads, "effective_decisions"), "current-support evaluator missing"
    base = matched_bundle()
    old = NOW - timedelta(days=6)
    record = base.records[0]
    record = record.model_copy(
        update={"modified": old, "document": {**record.document, "modified": old.isoformat()}}
    )
    aged = type(base)(
        answers=(base.answers[0].model_copy(update={"fetched_at": old}),), records=(record,)
    )
    _, catalog, spec, receipt, bundle = dependency_case(tmp_path, custom_bundle=aged)
    draft = FindingMaterializer(catalog).materialize(spec, (), dependency=receipt, bundle=bundle)

    class Registry:
        def bundle(self, digest):
            return bundle

    # Pure clock test only: live reads still obtain authoritative database_now.
    current = scan_reads.effective_decisions(draft, True, Registry(), NOW + timedelta(days=2))
    assert current[0].historical_disposition == "supported-static"
    assert current[0].effective_disposition == "unresolved"
    assert current[0].reason == "advisory_not_current"


@pytest.mark.parametrize("available", [False, True])
def test_current_support_never_promotes_historical_unresolved(tmp_path, available):
    from app.services import scan_reads

    assert hasattr(scan_reads, "effective_decisions"), "current-support evaluator missing"
    _, catalog, spec, receipt, bundle = dependency_case(tmp_path)
    draft = FindingMaterializer(catalog).materialize(spec, (), dependency=receipt, bundle=bundle)
    decisions = tuple(d.model_copy(update={"disposition": "unresolved"}) for d in draft.decisions)
    draft = type(draft).model_validate({**draft.model_dump(), "decisions": decisions})
    result = scan_reads.effective_decisions(draft, available, None, NOW)
    assert result[0].effective_disposition == "unresolved"
