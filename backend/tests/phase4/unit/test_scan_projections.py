"""Projection rows retain observations separately from selected findings."""

import importlib

from tests.phase4.unit.test_findings import dependency_case, native_case


def project(draft):
    assert importlib.util.find_spec("app.contracts.scan_publication") is not None, (
        "scan projections missing"
    )
    return importlib.import_module("app.contracts.scan_publication").project_scan(draft)


def test_native_projection_keeps_exact_observation_and_scoped_support(tmp_path):
    module, catalog, spec, receipt = native_case(tmp_path)
    draft = module.FindingMaterializer(catalog).materialize(spec, (receipt,))
    value = project(draft)
    assert len(value.observations) == len(value.findings) == len(value.support) == 1
    assert value.findings[0].observation_key == value.observations[0].observation_key
    assert value.batch.nodes == value.batch.edges == value.batch.derivations == []
    assert value.batch.evidence == list(draft.evidence)
    assert value.batch.coverage.admitted_count == value.batch.coverage.processed_count == 1
    assert value.batch_hash == value.batch.semantic_hash()


def test_dependency_empty_inventory_is_not_an_invented_finding(tmp_path):
    module, catalog, spec, receipt, bundle = dependency_case(tmp_path, "empty")
    draft = module.FindingMaterializer(catalog).materialize(
        spec, (), dependency=receipt, bundle=bundle
    )
    value = project(draft)
    assert len(value.observations) == 1
    assert value.observations[0].kind == "dependency_instance"
    assert value.findings == value.support == ()
    assert value.batch.disposition == "completed"
