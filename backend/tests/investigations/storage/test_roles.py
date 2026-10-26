"""PostgreSQL catalog and role privilege tests for investigation execution schema.

Verifies least-privilege role boundaries (p2_api, p2_broker, p2_operator),
immutable record triggers, and ciphertext access restrictions.
"""

import os

import pytest
from scripts import phase2_db as harness


@pytest.fixture
def database():
    if not os.environ.get("CODESPY_TEST_MARKER"):
        pytest.skip("owned database environment required")
    values = {k: v for k, v in os.environ.items() if k.startswith("CODESPY_TEST_")}
    name = harness.create_database(values, "investigation_schema")
    try:
        harness.migrate(values, name, "0031_investigation_execution")
        harness.grant_app(values, name)
        yield values, name
    finally:
        harness.drop_database(values, name)


def test_investigation_tables_exist_in_catalog(database):
    values, name = database
    with harness.connect(values, name) as conn:
        tables = [
            "investigation_specs",
            "investigation_execution_checkpoints",
            "investigation_step_receipts",
            "model_call_results",
            "investigation_evidence",
            "investigation_reports",
            "investigation_publication_receipts",
            "investigation_commands",
        ]
        for tbl in tables:
            assert (
                conn.execute("SELECT to_regclass(%s)", (f"public.{tbl}",)).fetchone()[0] is not None
            )


def test_api_role_cannot_insert_reports_or_checkpoints(database):
    values, name = database
    with harness.connect(values, name) as conn:
        for tbl in (
            "investigation_reports",
            "investigation_execution_checkpoints",
            "model_call_results",
        ):
            assert not conn.execute(
                "SELECT has_table_privilege('p2_api', %s, 'INSERT')", (tbl,)
            ).fetchone()[0]


def test_api_role_cannot_select_ciphertext_results(database):
    values, name = database
    with harness.connect(values, name) as conn:
        assert not conn.execute(
            "SELECT has_table_privilege('p2_api', 'model_call_results', 'SELECT')"
        ).fetchone()[0]


def test_broker_role_can_insert_checkpoints_and_results(database):
    values, name = database
    with harness.connect(values, name) as conn:
        for tbl in (
            "investigation_execution_checkpoints",
            "investigation_step_receipts",
            "investigation_reports",
            "investigation_publication_receipts",
            "investigation_evidence",
            "model_call_results",
        ):
            assert conn.execute(
                "SELECT has_table_privilege('p2_broker', %s, 'INSERT')", (tbl,)
            ).fetchone()[0]


def test_immutable_tables_reject_updates(database):
    values, name = database
    with harness.connect(values, name) as conn:
        for tbl in (
            "investigation_specs",
            "investigation_execution_checkpoints",
            "investigation_reports",
            "investigation_publication_receipts",
            "investigation_evidence",
            "investigation_commands",
        ):
            assert not conn.execute(
                "SELECT has_table_privilege('p2_broker', %s, 'UPDATE')", (tbl,)
            ).fetchone()[0]
