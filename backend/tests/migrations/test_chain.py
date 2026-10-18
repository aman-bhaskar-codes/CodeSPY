from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from app.db.base import Base
from app.models import tables  # noqa: F401 -- register current metadata for drift comparison
from scripts import phase1_db as db
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool


def test_fresh_chain_drift_and_catalog(db_values):
    name = db_values["CODESPY_TEST_DATABASE"]
    engine = create_engine(
        db.url(db_values, name, "p1_migration", db_values["CODESPY_TEST_MIGRATION_PASSWORD"]),
        poolclass=NullPool,
    )
    try:
        with engine.connect() as c:
            assert c.scalar(text("SELECT version_num FROM alembic_version")) == "0004_guards"
            actual = set(
                c.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
            )
            assert actual == set(Base.metadata.tables) | {"alembic_version"}
            assert compare_metadata(MigrationContext.configure(c), Base.metadata) == []
            triggers = (
                c.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))
                .scalars()
                .all()
            )
            assert len(triggers) == 15
            funcs = c.execute(
                text("SELECT proname,prosecdef FROM pg_proc WHERE proname LIKE 'codespy_%'")
            ).all()
            assert len(funcs) == 5 and all(not f[1] for f in funcs)
            checks = c.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_run_times'"  # noqa: E501
                )
            )
            assert "finished_at IS NOT NULL" in checks
    finally:
        engine.dispose()


def test_history_roundtrip_preserves_source(db_values):
    name = db.create_database(db_values, "migration")
    try:
        db.migrate(db_values, name, "0002_source")
        from uuid import uuid4

        org, _principal, repo, artifact, snapshot = [uuid4() for _ in range(5)]
        with db.connect(db_values, name) as c:
            c.execute(
                "INSERT INTO organizations(id,slug,display_name) VALUES (%s,%s,%s)",
                (org, "history", "History"),
            )
            c.execute(
                "INSERT INTO repositories(org_id,id,repository_key,display_name,source_kind) VALUES (%s,%s,%s,%s,%s)",  # noqa: E501
                (org, repo, "fixture", "Fixture", "local_fixture"),
            )
            c.execute(
                "INSERT INTO artifacts(org_id,id,repository_id,sha256,size_bytes,storage_key) VALUES (%s,%s,%s,%s,0,%s)",  # noqa: E501
                (org, artifact, repo, "a" * 64, "fixture"),
            )
            c.execute(
                "INSERT INTO source_snapshots(org_id,id,repository_id,artifact_id,admission_config_hash,identity_hash) VALUES (%s,%s,%s,%s,%s,%s)",  # noqa: E501
                (org, snapshot, repo, artifact, "b" * 64, "c" * 64),
            )
        db.migrate(db_values, name)
        db.migrate(db_values, name, "0002_source", True)
        with db.connect(db_values, name) as c:
            assert c.execute("SELECT id FROM source_snapshots").fetchall() == [(snapshot,)]
        db.migrate(db_values, name)
        db.migrate(db_values, name, "base", True)
        with db.connect(db_values, name) as c:
            assert (
                c.execute("SELECT count(*) FROM pg_extension WHERE extname='vector'").fetchone()[0]
                == 1
            )
        db.migrate(db_values, name)
    finally:
        db.drop_database(db_values, name)


def test_revisions_are_frozen():
    root = Path(__file__).resolve().parents[2] / "alembic/versions"
    for file in root.glob("*.py"):
        source = file.read_text()
        assert (
            "app.models" not in source
            and "metadata.create_all" not in source
            and "metadata.drop_all" not in source
        )
