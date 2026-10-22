"""Actual PostgreSQL catalog, digest checks and application-role privileges."""

from scripts import phase2_db as harness


def test_scoped_tables_and_privileges(db_values):
    with harness.connect(db_values, db_values["CODESPY_TEST_DATABASE"]) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM pg_tables WHERE schemaname='public' "
                "AND tablename<>'alembic_version'"
            ).fetchone()[0]
            == 38
        )
        assert connection.execute(
            "SELECT has_table_privilege('p2_api','scan_specs','INSERT')"
        ).fetchone()[0]
        for table in (
            "scan_reports",
            "scan_attempt_specs",
            "finding_occurrences",
            "finding_support",
            "verification_decisions",
        ):
            assert not connection.execute(
                "SELECT has_table_privilege('p2_api',%s,'INSERT')", (table,)
            ).fetchone()[0]
            assert connection.execute(
                "SELECT has_table_privilege('p2_broker',%s,'INSERT')", (table,)
            ).fetchone()[0]
            assert not connection.execute(
                "SELECT has_table_privilege('p2_broker',%s,'UPDATE')", (table,)
            ).fetchone()[0]
        assert not connection.execute(
            "SELECT has_table_privilege('p2_broker','scan_specs','INSERT')"
        ).fetchone()[0]
        assert (
            connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='jobs' AND column_name='scan_spec_id'"
            ).fetchone()
            is not None
        )


def test_json_unique_keys_and_digest_checks_exist(db_values):
    with harness.connect(db_values, db_values["CODESPY_TEST_DATABASE"]) as connection:
        assert (
            connection.execute(
                'SELECT \'{"a":1,"a":2}\' IS JSON OBJECT WITH UNIQUE KEYS'
            ).fetchone()[0]
            is False
        )
        definitions = connection.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='scan_reports'::regclass"
        ).fetchall()
        assert any("sha256" in row[0] for row in definitions)


def test_empty_scan_storage_roundtrip(db_values):
    name = db_values["CODESPY_TEST_DATABASE"]
    harness.migrate(db_values, name, "0011_structural_authority", downgrade=True)
    with harness.connect(db_values, name) as connection:
        assert connection.execute("SELECT to_regclass('public.scan_reports')").fetchone()[0] is None
    harness.migrate(db_values, name, "0012_scan_storage")
