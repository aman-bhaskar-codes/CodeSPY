"""Trusted capabilities reject unregistered identities before creating ownership."""

import importlib
from unittest.mock import Mock

import pytest
from app.scanners.finding_catalog import reviewed_catalog
from app.scanners.runtime import RuntimeManifest
from tests.phase4.runtime_fixtures import runtime
from tests.phase4.unit.test_scan_config import config


def test_registry_checks_exact_runtime_catalog_and_profile_before_execution(tmp_path):
    assert importlib.util.find_spec("app.services.scan_capabilities") is not None, (
        "trusted scan registry missing"
    )
    value, _ = config(tmp_path)
    catalog = reviewed_catalog(tuple(s.profile for s in value.native_selections))
    owner = Mock(side_effect=AssertionError("validation must not create resources"))
    registry = importlib.import_module("app.services.scan_capabilities").LocalScanCapabilities(
        RuntimeManifest(tools=(runtime("semgrep"),)),
        "sha256:" + value.runtime_digest,
        {catalog.semantic_hash(): catalog},
        {},
        Mock(),
        Mock(),
        owner,
        ("trusted-worker",),
    )
    registry.validate(value)
    with pytest.raises(ValueError):
        registry.validate(value.model_copy(update={"runtime_digest": "f" * 64}))
    with pytest.raises(ValueError):
        registry.validate(value.model_copy(update={"catalog_digest": "f" * 64}))
    owner.assert_not_called()
