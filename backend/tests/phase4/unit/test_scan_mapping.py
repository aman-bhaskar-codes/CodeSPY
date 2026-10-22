"""Storage mapping exposes scoped references without changing legacy projections."""

import importlib

from sqlalchemy import ForeignKeyConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable


def test_scan_storage_uses_scoped_keys_and_canonical_byte_integrity():
    assert importlib.util.find_spec("app.models.scan_tables") is not None, "scan storage missing"
    model = importlib.import_module("app.models.scan_tables")
    assert len(model.SCAN_TABLES) == 7
    for name, table in model.SCAN_TABLES.items():
        assert "org_id" in table.primary_key.columns
        fks = [c for c in table.constraints if isinstance(c, ForeignKeyConstraint)]
        assert all(len(c.columns) >= 2 for c in fks)
        sql = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        if name != "finding_support":
            assert "sha256" in sql and "WITH UNIQUE KEYS" in sql
    assert "scan_spec_id" in model.scan_jobs.c
    from app.models.phase2_tables import jobs

    assert "scan_spec_id" not in jobs.c
