"""Structural tests opt into the expanded schema in isolated owned databases."""

import os

import pytest
from tests.phase2.conftest import engine, factory  # noqa: F401


@pytest.fixture
def db_values(monkeypatch):
    from scripts import phase2_db as harness

    values = {key: value for key, value in os.environ.items() if key.startswith("CODESPY_TEST_")}
    if not values.get("CODESPY_TEST_MARKER"):
        pytest.skip("owned database environment required")
    database = harness.create_database(values, "phase3case")
    try:
        harness.migrate(values, database, "0011_structural_authority")
        harness.grant_app(values, database)
        yield {**values, "CODESPY_TEST_DATABASE": database}
    finally:
        harness.drop_database(values, database)
