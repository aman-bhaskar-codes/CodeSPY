"""Reusable configuration never embeds a run identity or its own digest."""

import importlib
from uuid import UUID

import pytest

from tests.phase4.dependency_fixtures import NOW
from tests.phase4.unit.test_findings import native_case


def module():
    assert importlib.util.find_spec("app.contracts.scan_jobs") is not None, "scan config missing"
    return importlib.import_module("app.contracts.scan_jobs")


def config(tmp_path):
    _, _, spec, receipt = native_case(tmp_path)
    m = module()
    value = m.ScanConfigV1(
        runtime_digest=spec.runtime_digest,
        catalog_digest=spec.catalog_digest,
        native_selections=(
            m.NativeSelectionV1(profile=receipt.request.profile, files=receipt.request.files),
        ),
    )
    return value, spec


def test_fresh_attempts_derive_distinct_run_specs_from_one_stable_config(tmp_path):
    value, original = config(tmp_path)
    first = value.publication_spec(original.scope, original.repository_id, NOW)
    second = value.publication_spec(
        original.scope.model_copy(update={"run_id": UUID(int=999)}), original.repository_id, NOW
    )
    assert first.config_hash == second.config_hash == value.semantic_hash()
    assert first.native_requests[0].scope.run_id != second.native_requests[0].scope.run_id
    assert "scope" not in value.model_dump() and "config_hash" not in value.model_dump()


def test_config_rejects_duplicate_operations_and_self_referential_fields(tmp_path):
    value, _ = config(tmp_path)
    with pytest.raises(ValueError):
        type(value).model_validate(
            {**value.model_dump(), "native_selections": value.native_selections * 2}
        )
    with pytest.raises(ValueError):
        type(value).model_validate({**value.model_dump(), "config_hash": "a" * 64})
    with pytest.raises(ValueError):
        type(value).model_validate({**value.model_dump(), "native_selections": ()})


def test_stored_json_roundtrip_preserves_typed_request_metadata(tmp_path):
    value, _ = config(tmp_path)
    assert module().stored_config(value.model_dump(mode="json")) == value


def test_native_selection_limits_and_order_are_configuration_identity(tmp_path):
    value, _ = config(tmp_path)
    selection = value.native_selections[0]
    first = selection.files[0]
    files = tuple(
        first.model_copy(update={"file_id": UUID(int=i + 100), "size_bytes": 131072})
        for i in range(128)
    )
    selection = type(selection)(profile=selection.profile, files=files)
    reverse = type(selection)(profile=selection.profile, files=files[::-1])
    assert selection == reverse
    with pytest.raises(ValueError):
        type(selection)(profile=selection.profile, files=(*files, first))
    with pytest.raises(ValueError):
        type(selection)(
            profile=selection.profile,
            files=(files[0].model_copy(update={"size_bytes": 131073}), *files[1:]),
        )


def test_cross_tool_conflicting_metadata_cannot_share_a_file_identity(tmp_path):
    value, _ = config(tmp_path)
    selection = value.native_selections[0]
    other = selection.model_copy(
        update={
            "profile": selection.profile.model_copy(update={"tool": "gitleaks"}),
            "files": (selection.files[0].model_copy(update={"sha256": "f" * 64}),),
        }
    )
    with pytest.raises(ValueError):
        type(value)(
            runtime_digest=value.runtime_digest,
            catalog_digest=value.catalog_digest,
            native_selections=(selection, other),
        )
