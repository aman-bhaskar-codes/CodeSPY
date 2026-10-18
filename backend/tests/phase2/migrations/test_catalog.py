from sqlalchemy import text


async def test_additive_catalog_and_roles(engine):
    async with engine.connect() as conn:
        assert (
            await conn.scalar(text("SELECT version_num FROM alembic_version"))
            == "0008_worker_fencing"
        )
        names = set(
            (
                await conn.scalars(
                    text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                )
            ).all()
        )
        assert len(names - {"alembic_version"}) == 28
        assert not await conn.scalar(
            text("SELECT has_table_privilege('p2_api','source_files','INSERT')")
        )
        assert not await conn.scalar(
            text("SELECT has_schema_privilege('p2_broker','public','CREATE')")
        )


async def test_round_trip_preserves_phase1_snapshot(factory, engine, db_values):
    from scripts import phase2_db as harness
    from scripts.phase1_fixture import scenario

    _, ctx, snapshot, _, _ = await scenario(factory)
    await engine.dispose()
    database = db_values["CODESPY_TEST_DATABASE"]
    harness.migrate(db_values, database, "0004_guards", downgrade=True)
    harness.migrate(db_values, database)
    async with engine.connect() as conn:
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM source_snapshots WHERE org_id=:org AND id=:id"),
                {"org": ctx.org_id, "id": snapshot.id},
            )
            == 1
        )
