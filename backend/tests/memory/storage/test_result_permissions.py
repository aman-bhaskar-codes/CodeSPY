"""Retained model ciphertext is readable only through scoped operations."""

from scripts import phase2_db as harness


def test_broker_cannot_select_model_result_ciphertext(db_values):
    with harness.connect(db_values, db_values["CODESPY_TEST_DATABASE"]) as connection:
        assert not connection.execute(
            "SELECT has_table_privilege('p2_broker','public.model_call_results','SELECT')"
        ).fetchone()[0]


async def test_scoped_result_replay_and_revocation(migration_factory, broker_factory, tmp_path):
    from app.contracts.investigation_execution import ModelInvocationRefV1
    from app.models.investigation_tables import investigations
    from app.models.model_gateway_tables import model_reservations
    from app.models.phase2_tables import PHASE2_TABLES
    from app.models.tables import source_snapshots
    from app.services.model_call_results import SqlModelResultStore
    from app.services.model_continuations import ContinuationCipher
    from sqlalchemy import select
    from tests.model_connections.storage.test_configured_gateway import configured

    gateway, args, client, calls = await configured(migration_factory, tmp_path)
    store = SqlModelResultStore(broker_factory, ContinuationCipher(bytes(range(32))))
    gateway.result_store = store
    async with migration_factory() as session, session.begin():
        snapshot = await session.scalar(
            select(source_snapshots.c.id).where(
                source_snapshots.c.org_id == args["org_id"],
                source_snapshots.c.repository_id == args["repository_id"],
            )
        )
        await session.execute(
            investigations.insert().values(
                org_id=args["org_id"],
                id=args["investigation_id"],
                repository_id=args["repository_id"],
                principal_id=args["principal_id"],
                snapshot_id=snapshot,
                target_type="repository",
                query="Synthetic result replay",
                budget_limit_micros=100000,
            )
        )
    try:
        response = await gateway.execute_call(**args)
        assert await gateway.execute_call(**args) == response
        assert len(calls) == 1
        async with migration_factory() as session:
            reservation = (
                (
                    await session.execute(
                        select(model_reservations).where(
                            model_reservations.c.org_id == args["org_id"]
                        )
                    )
                )
                .mappings()
                .one()
            )
        invocation = ModelInvocationRefV1(
            org_id=args["org_id"],
            repository_id=args["repository_id"],
            principal_id=args["principal_id"],
            investigation_id=args["investigation_id"],
            node_id=reservation["logical_call_id"],
            reservation_id=reservation["reservation_id"],
            request_digest=reservation["request_digest"],
            profile_digest="a" * 64,
        )
        # Use the retained profile binding produced by the gateway, not a guessed revision.
        from app.models.investigation_execution_tables import model_call_results

        async with migration_factory() as session:
            stored = (await session.execute(select(model_call_results))).mappings().one()
            profile = stored["profile_revision"]
        invocation = invocation.model_copy(
            update={"profile_digest": profile, "request_digest": stored["request_digest"]}
        )
        from uuid import uuid4

        assert await store.get(invocation.model_copy(update={"repository_id": uuid4()})) is None
        async with migration_factory() as session, session.begin():
            await session.execute(
                PHASE2_TABLES["repository_grants"]
                .update()
                .where(PHASE2_TABLES["repository_grants"].c.org_id == args["org_id"])
                .values(permission="read")
            )
        assert await store.get(invocation) is None
        async with broker_factory() as session, session.begin():
            assert await store.cleanup_expired(session, 10) == 0
    finally:
        await client.aclose()
