"""Scan-specific guards are additive and cannot be bypassed by trusted API roles."""

import pytest
from scripts import phase2_db as harness


def test_scan_guard_functions_and_immutable_triggers_are_installed(db_values):
    with harness.connect(db_values, db_values["CODESPY_TEST_DATABASE"]) as connection:
        for function in (
            "codespy_scan_result_guard",
            "codespy_scan_complete_set",
            "codespy_scan_committed_guard",
        ):
            assert (
                connection.execute(
                    "SELECT to_regprocedure(%s)",
                    (
                        function + "()"
                        if function != "codespy_scan_complete_set"
                        else function + "(uuid,uuid,uuid)",
                    ),
                ).fetchone()[0]
                is not None
            )
        assert (
            connection.execute(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgname LIKE 'guard_scan_%' AND NOT tgisinternal"
            ).fetchone()[0]
            >= 7
        )


def test_api_cannot_insert_scan_report(db_values):
    with (
        harness.connect(
            db_values,
            db_values["CODESPY_TEST_DATABASE"],
            "p2_api",
            db_values["CODESPY_TEST_APP_PASSWORD"],
        ) as connection,
        pytest.raises(Exception, match="permission denied"),
    ):
        connection.execute("INSERT INTO scan_reports DEFAULT VALUES")
