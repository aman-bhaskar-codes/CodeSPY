"""Owned scan runner rejects invalid destinations before any Docker/database action."""

import importlib

import pytest


def test_scan_runner_rejects_nonowned_destination(tmp_path):
    assert importlib.util.find_spec("scripts.scan_db") is not None, "scan harness missing"
    harness = importlib.import_module("scripts.scan_db")
    path = tmp_path / "invalid.env"
    path.write_text("CODESPY_TEST_HOST=remote.invalid\nCODESPY_TEST_PORT=5432\n")
    with pytest.raises(ValueError, match="loopback"):
        harness.read_env(path)


def test_existing_prepare_accepts_explicit_postgres_only_without_changing_default():
    import inspect

    from scripts import phase2_db

    assert "services" in inspect.signature(phase2_db.prepare).parameters, (
        "selective preparation missing"
    )
    assert inspect.signature(phase2_db.prepare).parameters["services"].default == (
        "postgres",
        "localstack",
    )
