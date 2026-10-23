"""Real migration ledger and grant-role compatibility on an owned cluster."""

from app.db.migration_version import ensure_revision_capacity
from scripts import phase2_db as harness
from sqlalchemy import text


async def test_existing_short_revision_column_is_widened_without_changing_identity(
    migration_factory,
):
    async with migration_factory() as session, session.begin():
        before = await session.scalar(text("SELECT version_num FROM public.alembic_version"))
        await session.execute(
            text("ALTER TABLE public.alembic_version ALTER COLUMN version_num TYPE varchar(32)")
        )
        connection = await session.connection()
        await connection.run_sync(ensure_revision_capacity)
        assert (
            await session.scalar(text("SELECT version_num FROM public.alembic_version")) == before
        )
        assert (
            await session.scalar(
                text(
                    "SELECT character_maximum_length FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='alembic_version' "
                    "AND column_name='version_num'"
                )
            )
            == 64
        )


def test_current_head_round_trip_preserves_revision_ids_and_inert_role(db_values):
    harness.verify_marker(db_values, db_values["CODESPY_TEST_DATABASE"])
    harness.provision_compatibility_role(db_values)
    with harness.connect(db_values) as connection:
        assert connection.execute(
            "SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolinherit,rolbypassrls "
            "FROM pg_roles WHERE rolname='codespy_app'"
        ).fetchone() == (False, False, False, False, False, False)
        assert connection.execute(
            "SELECT pg_has_role('p2_api','codespy_app','MEMBER'), "
            "pg_has_role('p2_broker','codespy_app','MEMBER')"
        ).fetchone() == (False, False)
    name = db_values["CODESPY_TEST_DATABASE"]
    harness.migrate(db_values, name, "0022_triage_commands")
    harness.migrate(db_values, name, "0021_investigations_and_checkpoints", downgrade=True)
    harness.migrate(db_values, name, "0022_triage_commands")
    with harness.connect(db_values, name) as connection:
        assert connection.execute("SELECT version_num FROM public.alembic_version").fetchone() == (
            "0022_triage_commands",
        )
