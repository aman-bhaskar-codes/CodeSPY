"""Owner reconciliation on disposable PostgreSQL with simulated usage receipts."""

import asyncio

import pytest
from app.adapters.model.fake import FakeFailureMode, FakeModelAdapter, FakeModelTimeoutError
from app.contracts.model import TokenUsageV1
from app.contracts.model_reconciliation import ReconcileModelUsageV1
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.domain.scope import OrgRole, TenantContext
from app.models.model_gateway_tables import (
    model_reservations,
    model_spend_ledger,
    model_usage_reconciliations,
)
from app.models.tables import memberships
from app.services.model_budget import ModelBudgetService
from app.services.model_gateway import ModelGatewayService
from app.services.model_reconciliation import ModelReconciliationService
from app.services.model_router import ModelRouter
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from tests.accounting.storage.test_durable_model_gateway import prepared


async def uncertain(factory, tmp_path):
    args = await prepared(factory, tmp_path)
    async with factory() as session:
        budget = ModelBudgetService(session)
        gateway = ModelGatewayService(
            ModelRouter(budget),
            budget,
            {"fake": FakeModelAdapter(failure_mode=FakeFailureMode.TIMEOUT)},
            accounting_factory=factory,
        )
        with pytest.raises(FakeModelTimeoutError):
            await gateway.execute_call(**args)
    async with factory() as session:
        row = (await session.execute(select(model_reservations))).mappings().one()
        assert row["state"] == "uncertain_hold"
    ctx = TenantContext(
        org_id=args["org_id"], principal_id=args["principal_id"], role=OrgRole.OWNER
    )
    request = ReconcileModelUsageV1(
        reservation_id=row["reservation_id"],
        request_key="receipt-one",
        provider_request_id="provider-receipt",
        evidence_digest="a" * 64,
        usage=TokenUsageV1(input_tokens=10, output_tokens=5, cache_read_tokens=3),
        reason="provider_usage_receipt",
    )
    return args, ctx, request


async def test_concurrent_reconciliation_charges_once(migration_factory, tmp_path):
    args, ctx, request = await uncertain(migration_factory, tmp_path)
    service = ModelReconciliationService(migration_factory)
    results = await asyncio.gather(
        *(service.reconcile(ctx, args["repository_id"], request) for _ in range(2))
    )
    assert results[0] == results[1]
    async with migration_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(model_usage_reconciliations)) == 1
        )
        rows = (
            (
                await session.execute(
                    select(model_spend_ledger).where(
                        model_spend_ledger.c.is_uncertain_hold.is_(False)
                    )
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 1
        assert rows[0]["cache_read_tokens"] == 3
        assert rows[0]["charged_micros"] == results[0].charged_micros
    with pytest.raises(DuplicateConflict):
        await service.reconcile(
            ctx, args["repository_id"], request.model_copy(update={"evidence_digest": "b" * 64})
        )


async def test_reconciliation_checks_current_owner_and_audit_is_immutable(
    migration_factory, tmp_path
):
    args, ctx, request = await uncertain(migration_factory, tmp_path)
    service = ModelReconciliationService(migration_factory)
    await service.reconcile(ctx, args["repository_id"], request)
    async with migration_factory() as session, session.begin():
        await session.execute(
            update(memberships).where(memberships.c.org_id == ctx.org_id).values(role="admin")
        )
    with pytest.raises(ScopeViolation):
        await service.reconcile(ctx, args["repository_id"], request)
    async with migration_factory() as session:
        for sql in (
            "UPDATE model_usage_reconciliations SET evidence_digest=repeat('b',64)",
            "DELETE FROM model_usage_reconciliations",
        ):
            with pytest.raises(DBAPIError):
                await session.execute(text(sql))
            await session.rollback()


async def test_audit_failure_rolls_back_charge(migration_factory, tmp_path):
    args, ctx, request = await uncertain(migration_factory, tmp_path)
    async with migration_factory() as session, session.begin():
        await session.execute(
            text("""
CREATE FUNCTION reject_test_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'injected audit failure' USING ERRCODE='23514'; END $$;
""")
        )
        await session.execute(
            text("""
CREATE TRIGGER reject_test_receipt BEFORE INSERT ON model_usage_reconciliations
FOR EACH ROW EXECUTE FUNCTION reject_test_receipt();
""")
        )
    from app.domain.errors import InvalidTransition

    with pytest.raises(InvalidTransition):
        await ModelReconciliationService(migration_factory).reconcile(
            ctx, args["repository_id"], request
        )
    async with migration_factory() as session:
        row = (await session.execute(select(model_reservations))).mappings().one()
        assert row["state"] == "uncertain_hold"
        assert (
            await session.scalar(select(func.count()).select_from(model_usage_reconciliations)) == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(model_spend_ledger)
                .where(model_spend_ledger.c.is_uncertain_hold.is_(False))
            )
            == 0
        )


async def test_reconciliation_uses_captured_price_and_preserves_history(
    migration_factory, db_values, tmp_path
):
    from scripts import phase2_db as harness

    args, ctx, request = await uncertain(migration_factory, tmp_path)
    async with migration_factory() as session, session.begin():
        price = await ModelBudgetService(session).get_reserved_price(
            ctx.org_id, request.reservation_id
        )
        expected = price.calculate_cost_micros(**request.usage.model_dump())
        await session.execute(text("UPDATE price_schedules SET input_micros_per_1k=999999"))
    result = await ModelReconciliationService(migration_factory).reconcile(
        ctx, args["repository_id"], request
    )
    assert result.charged_micros == expected
    with pytest.raises(DBAPIError, match="cannot downgrade usage reconciliation history"):
        harness.migrate(
            db_values, db_values["CODESPY_TEST_DATABASE"], "0026_model_cache_usage", downgrade=True
        )


async def test_revoked_repository_grant_denies_reconciliation(migration_factory, tmp_path):
    args, ctx, request = await uncertain(migration_factory, tmp_path)
    async with migration_factory() as session, session.begin():
        await session.execute(text("UPDATE repository_grants SET revoked_at=clock_timestamp()"))
    with pytest.raises(ScopeViolation):
        await ModelReconciliationService(migration_factory).reconcile(
            ctx, args["repository_id"], request
        )
    async with migration_factory() as session:
        assert await session.scalar(select(model_reservations.c.state)) == "uncertain_hold"


async def test_forged_audit_insert_rejected(migration_factory, tmp_path):
    args, ctx, request = await uncertain(migration_factory, tmp_path)
    service = ModelReconciliationService(migration_factory)
    await service.reconcile(ctx, args["repository_id"], request)
    async with migration_factory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text("""
INSERT INTO model_usage_reconciliations
SELECT org_id, gen_random_uuid(), repository_id, reservation_id, actor_principal_id,
 'forged', request_digest, evidence_digest, price_digest, provider_request_id,
 usage_json, reason, charged_micros+1, created_at FROM model_usage_reconciliations
""")
            )


async def test_settled_call_rejects_new_reconciliation_key(migration_factory, tmp_path):
    from app.domain.errors import InvalidTransition

    args, ctx, request = await uncertain(migration_factory, tmp_path)
    service = ModelReconciliationService(migration_factory)
    await service.reconcile(ctx, args["repository_id"], request)
    with pytest.raises(InvalidTransition):
        await service.reconcile(
            ctx,
            args["repository_id"],
            request.model_copy(update={"request_key": "another-receipt"}),
        )
