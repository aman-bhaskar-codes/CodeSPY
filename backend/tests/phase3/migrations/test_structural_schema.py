# ruff: noqa: E501
"""Head-only tests use explicit owned harness; historical catalog stays at 0009."""

import os
from uuid import uuid4

import pytest
from scripts import phase2_db as harness


@pytest.fixture
def database():
    if not os.environ.get("CODESPY_TEST_MARKER"):
        pytest.skip("owned database environment required")
    values = {k: v for k, v in os.environ.items() if k.startswith("CODESPY_TEST_")}
    name = harness.create_database(values, "structure")
    try:
        harness.migrate(values, name, "0011_structural_authority")
        harness.grant_app(values, name)
        yield values, name
    finally:
        harness.drop_database(values, name)


def test_head_catalog_and_privileges(database):
    values, name = database
    with harness.connect(values, name) as connection:
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0011_structural_authority"
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename<>'alembic_version'"
            ).fetchone()[0]
            == 31
        )
        for table, role in [("analysis_specs", "p2_api"), ("structural_reports", "p2_broker")]:
            assert connection.execute(
                "SELECT has_table_privilege(%s,%s,'INSERT')", (role, table)
            ).fetchone()[0]
        for table in ["structural_reports", "cpg_nodes", "cpg_edges", "evidence_records"]:
            assert not connection.execute(
                "SELECT has_table_privilege('p2_api',%s,'INSERT')", (table,)
            ).fetchone()[0]
        assert not connection.execute(
            "SELECT has_function_privilege('p2_api','codespy_phase2_authority()','EXECUTE')"
        ).fetchone()[0]


def test_raw_report_without_live_authority_rejected(database):
    values, name = database
    with (
        harness.connect(values, name) as connection,
        pytest.raises(Exception, match="worker context required"),
    ):
        connection.execute(
            "INSERT INTO structural_reports(org_id,snapshot_id,run_id,spec_id,repository_id,config_hash,report_json,report_sha256,normalized_sha256) VALUES(%s,%s,%s,%s,%s,%s,'{}',%s,%s)",
            (*[uuid4() for _ in range(5)], "a" * 64, "a" * 64, "a" * 64),
        )


def test_empty_downgrade_preserves_historical_catalog(database):
    values, name = database
    harness.migrate(values, name, "0009_api_commands", downgrade=True)
    with harness.connect(values, name) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename<>'alembic_version'"
            ).fetchone()[0]
            == 29
        )
    harness.migrate(values, name, "0011_structural_authority")
