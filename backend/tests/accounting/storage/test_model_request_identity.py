"""Additive request identity migration and database immutability boundaries."""

import pytest
from app.adapters.model.fake import FakeModelAdapter
from app.models.model_gateway_tables import model_reservations
from app.services.model_budget import ModelBudgetService
from app.services.model_gateway import ModelGatewayService
from app.services.model_router import ModelRouter
from scripts import phase2_db as harness
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from tests.accounting.storage.test_durable_model_gateway import prepared


async def bound_call(factory, tmp_path):
    args = await prepared(factory, tmp_path)
    async with factory() as anchor:
        budget = ModelBudgetService(anchor)
        gateway = ModelGatewayService(
            ModelRouter(budget), budget, {"fake": FakeModelAdapter()}, accounting_factory=factory
        )
        await gateway.execute_call(**args)
    return args


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE model_reservations SET request_digest=repeat('a',64)",
        (
            "UPDATE model_reservations SET price_snapshot=price_snapshot || "
            '\'{"version":"changed"}\'::jsonb'
        ),
        "UPDATE model_reservations SET estimated_output_tokens=estimated_output_tokens+1",
        "DELETE FROM model_reservations",
    ],
)
async def test_bound_identity_rejects_raw_mutation(migration_factory, tmp_path, statement):
    args = await bound_call(migration_factory, tmp_path)
    async with migration_factory() as session:
        before = dict((await session.execute(select(model_reservations))).mappings().one())
        with pytest.raises(DBAPIError):
            await session.execute(text(statement))
        await session.rollback()
        after = dict((await session.execute(select(model_reservations))).mappings().one())
        assert before == after
        captured = await ModelBudgetService(session).get_reserved_price(
            args["org_id"], before["reservation_id"]
        )
        assert captured.version == before["price_snapshot"]["version"]


async def test_legacy_rows_survive_migration_roundtrip(migration_factory, db_values, tmp_path):
    args = await prepared(migration_factory, tmp_path)
    async with migration_factory() as session, session.begin():
        identity = await ModelBudgetService(session).reserve_generation_budget(
            **{
                key: args[key]
                for key in (
                    "org_id",
                    "repository_id",
                    "principal_id",
                    "investigation_id",
                    "logical_call_id",
                    "idempotency_key",
                )
            },
            attempt_id=1,
            provider="fake",
            model_id="offline",
            estimated_input_tokens=10,
            estimated_output_tokens=10,
            estimated_micros=100,
        )
    name = db_values["CODESPY_TEST_DATABASE"]
    harness.migrate(db_values, name, "0023_index_publication", downgrade=True)
    with harness.connect(db_values, name) as connection:
        assert connection.execute("SELECT count(*) FROM model_reservations").fetchone()[0] == 1
    harness.migrate(db_values, name, "0024_model_request_identity")
    async with migration_factory() as session:
        row = (await session.execute(select(model_reservations))).mappings().one()
        assert row["reservation_id"] == identity
        assert row["request_digest"] is None and row["price_snapshot"] is None
        assert row["reserved_micros"] == 100


@pytest.mark.parametrize("missing_price", [True, False])
async def test_new_identity_rejects_incomplete_metadata(migration_factory, tmp_path, missing_price):
    from uuid import uuid4

    await bound_call(migration_factory, tmp_path)
    async with migration_factory() as session:
        row = dict((await session.execute(select(model_reservations))).mappings().one())
        row.update(reservation_id=uuid4(), idempotency_key="invalid-clone")
        if missing_price:
            row["price_snapshot"] = None
            expected = "ck_model_request_pair"
        else:
            row["price_snapshot"] = {
                "provider": row["provider"],
                "model_id": row["model_id"],
                "version": None,
            }
            expected = "ck_model_price_scope"
        with pytest.raises(DBAPIError, match=expected):
            await session.execute(model_reservations.insert().values(**row))
        await session.rollback()


async def test_reserved_price_survives_global_schedule_change(migration_factory, tmp_path):
    from app.models.budget_tables import price_schedules
    from sqlalchemy import update

    args = await bound_call(migration_factory, tmp_path)
    async with migration_factory() as session, session.begin():
        row = (await session.execute(select(model_reservations))).mappings().one()
        service = ModelBudgetService(session)
        captured = await service.get_reserved_price(args["org_id"], row["reservation_id"])
        # Privileged corruption probe; normal service writes reject this version change.
        await session.execute(update(price_schedules).values(output_micros_per_1k=999999))
    async with migration_factory() as session:
        price = await ModelBudgetService(session).get_reserved_price(
            args["org_id"], row["reservation_id"]
        )
        assert price == captured


async def test_downgrade_refuses_to_erase_bound_identity(migration_factory, db_values, tmp_path):
    await bound_call(migration_factory, tmp_path)
    with pytest.raises(DBAPIError, match="cannot downgrade bound model accounting history"):
        harness.migrate(
            db_values, db_values["CODESPY_TEST_DATABASE"], "0023_index_publication", downgrade=True
        )
    async with migration_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
            db_values.get("CODESPY_TEST_SCHEMA_TARGET", "0024_model_request_identity")
        )
        assert await session.scalar(select(model_reservations.c.request_digest)) is not None
