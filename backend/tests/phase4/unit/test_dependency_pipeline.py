"""Archive-to-offline-evidence tests are not Docker containment qualification."""

import pytest
from app.contracts.ingestion import AdmissionPolicyV1
from app.scanners.advisories import AdvisoryBundle
from app.scanners.dependency_worker import DependencyInputV1, assess_dependencies

from tests.phase4.dependency_fixtures import fixture_dependency


def test_admitted_npm_empty_answer_is_complete_but_missing_bundle_is_partial(tmp_path):
    source, bundle = fixture_dependency(tmp_path)
    result = assess_dependencies(DependencyInputV1(source=source, bundle=bundle), tmp_path)
    result.validate_for(source.request)
    assert result.coverage == "complete"
    assert result.packages[0].assessment.status == "empty"
    source, bundle = fixture_dependency(tmp_path, bundle=AdvisoryBundle(answers=(), records=()))
    result = assess_dependencies(DependencyInputV1(source=source, bundle=bundle), tmp_path)
    assert result.coverage == "partial"
    assert result.packages[0].assessment.status == "missing"


def test_prior_excluded_uv_metadata_is_not_read_or_promoted(tmp_path):
    source, bundle = fixture_dependency(
        tmp_path, policy=AdmissionPolicyV1(), contents={"uv.lock": b"PRIVATE-CANARY"}
    )
    result = assess_dependencies(DependencyInputV1(source=source, bundle=bundle), tmp_path)
    assert result.coverage == "unknown"
    assert result.files[0].status == "excluded"
    assert result.packages == ()
    assert "PRIVATE-CANARY" not in result.model_dump_json()


def test_raw_markers_are_not_forwarded_into_result(tmp_path):
    raw = b"""version=1
revision=3
[[package]]
name="example"
version="1.2.3"
source={registry="https://pypi.org/simple"}
resolution-markers=["sys_platform == 'PRIVATE-CANARY'"]
"""
    source, bundle = fixture_dependency(tmp_path, contents={"uv.lock": raw})
    result = assess_dependencies(DependencyInputV1(source=source, bundle=bundle), tmp_path)
    assert result.packages[0].marker_count == 1
    assert "PRIVATE-CANARY" not in result.model_dump_json()


@pytest.mark.parametrize("bad", ["bundle", "archive"])
def test_mismatched_dependency_inputs_fail_closed(tmp_path, bad):
    source, bundle = fixture_dependency(tmp_path)
    if bad == "bundle":
        with pytest.raises(ValueError):
            DependencyInputV1(source=source, bundle=AdvisoryBundle(answers=(), records=()))
    else:
        (tmp_path / "archive.zip").write_bytes(b"corrupt")
        with pytest.raises(Exception, match="source_integrity_error"):
            assess_dependencies(DependencyInputV1(source=source, bundle=bundle), tmp_path)


def test_nested_and_aliased_instances_survive_exact_query_reuse(tmp_path):
    import json

    entries = {
        path: {
            "name": "example",
            "version": version,
            "resolved": "https://registry.npmjs.org/example/-/example-" + version + ".tgz",
        }
        for path, version in (
            ("node_modules/example", "1.2.3"),
            ("node_modules/other/node_modules/example", "1.0.0"),
            ("node_modules/alias", "1.2.3"),
        )
    }
    source, bundle = fixture_dependency(
        tmp_path,
        contents={
            "package-lock.json": json.dumps({"lockfileVersion": 3, "packages": entries}).encode()
        },
    )
    result = assess_dependencies(DependencyInputV1(source=source, bundle=bundle), tmp_path)
    assert len(result.packages) == 3
    assert len({p.location for p in result.packages}) == 3
    assert sorted(p.assessment.status for p in result.packages) == ["empty", "empty", "missing"]
    assert result.coverage == "partial"


def test_uv_marker_variants_are_distinct_and_never_claim_runtime_applicability(tmp_path):
    raw = b"""version=1
revision=3
[[package]]
name="example"
version="1.2.3"
source={registry="https://pypi.org/simple"}
resolution-markers=["sys_platform == 'linux'"]
[[package]]
name="example"
version="1.2.3"
source={registry="https://pypi.org/simple"}
resolution-markers=["sys_platform == 'darwin'"]
"""
    source, bundle = fixture_dependency(tmp_path, contents={"uv.lock": raw})
    result = assess_dependencies(DependencyInputV1(source=source, bundle=bundle), tmp_path)
    assert len(result.packages) == 2
    assert {p.location for p in result.packages} == {"package[0]", "package[1]"}
    assert all(
        p.marker_count == 1 and p.environment_status == "unresolved" for p in result.packages
    )
    assert "darwin" not in result.model_dump_json()


def test_private_sources_remain_unresolved_even_if_public_name_has_an_empty_answer(tmp_path):
    import json

    raw = json.dumps(
        {
            "lockfileVersion": 3,
            "packages": {
                "node_modules/example": {
                    "version": "1.2.3",
                    "resolved": "https://PRIVATE-CANARY.invalid/example.tgz",
                }
            },
        }
    ).encode()
    source, bundle = fixture_dependency(tmp_path, contents={"package-lock.json": raw})
    result = assess_dependencies(DependencyInputV1(source=source, bundle=bundle), tmp_path)
    assert result.packages == ()
    assert len(result.unresolved) == 1
    assert result.coverage == "partial"
    assert "PRIVATE-CANARY" not in result.model_dump_json()


def test_repeated_unresolved_uv_variants_keep_distinct_lock_locations(tmp_path):
    raw = b"""version=1
revision=3
[[package]]
name="private-example"
version="1.0"
source={registry="https://private.invalid/simple"}
resolution-markers=["sys_platform == 'linux'"]
[[package]]
name="private-example"
version="1.0"
source={registry="https://private.invalid/simple"}
resolution-markers=["sys_platform == 'darwin'"]
"""
    source, bundle = fixture_dependency(tmp_path, contents={"uv.lock": raw})
    result = assess_dependencies(DependencyInputV1(source=source, bundle=bundle), tmp_path)
    assert result.coverage == "partial"
    assert {p.location for p in result.unresolved} == {"package[0]", "package[1]"}


@pytest.mark.parametrize("count", [5000, 5001])
def test_aggregate_instance_budget_across_selected_metadata_files(tmp_path, count):
    import json

    contents = {}
    for i, size in enumerate((2000, 2000, count - 4000)):
        entries = {
            f"node_modules/item-{n}": {
                "name": "example",
                "version": "1.2.3",
                "resolved": "https://registry.npmjs.org/example/-/example.tgz",
            }
            for n in range(size)
        }
        contents[f"project-{i}/package-lock.json"] = json.dumps(
            {"lockfileVersion": 3, "packages": entries}
        ).encode()
    source, bundle = fixture_dependency(tmp_path, contents=contents)
    value = DependencyInputV1(source=source, bundle=bundle)
    if count > 5000:
        with pytest.raises(ValueError, match="instance budget"):
            assess_dependencies(value, tmp_path)
    else:
        result = assess_dependencies(value, tmp_path)
        assert len(result.packages) == 5000
        assert result.coverage == "complete"
