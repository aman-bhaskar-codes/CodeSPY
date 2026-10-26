"""Committed accounting around simulated inference on real PostgreSQL."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from app.adapters.model.fake import FakeModelAdapter
from app.contracts.model import ChatMessageV1, ModelRole, ModelRoutingRequirementV1
from app.contracts.provider_budget import DisclosurePolicyV1
from app.db.time import database_now
from app.models.model_gateway_tables import model_reservations
from app.models.tables import repositories
from app.services.model_budget import ModelBudgetService
from app.services.model_gateway import ModelGatewayService
from app.services.model_router import ModelRouter
from sqlalchemy import select, update

from tests.phase3.integration.test_structural_publication import admitted


async def prepared(factory, tmp_path):
    ctx, _snapshot, repo, _store = await admitted(factory, tmp_path)
    async with factory() as session, session.begin():
        budget = ModelBudgetService(session)
        await budget.ensure_budget(ctx.org_id, repo, 10000)
        await budget.ensure_default_generation_pricing()
    return {
        "org_id": ctx.org_id,
        "repository_id": repo,
        "principal_id": ctx.principal_id,
        "investigation_id": uuid4(),
        "logical_call_id": uuid4(),
        "messages": (ChatMessageV1(role=ModelRole.USER, content="synthetic test"),),
        "requirement": ModelRoutingRequirementV1(
            estimated_context_tokens=100, estimated_output_tokens=100
        ),
        "max_tokens": 100,
        "disclosure_policy": DisclosurePolicyV1(allow_external_disclosure=False),
        "idempotency_key": "durable-one",
    }


@pytest.mark.parametrize("outcome", ["success", "exception", "cancel"])
async def test_accounting_survives_outer_rollback(migration_factory, tmp_path, outcome):
    args = await prepared(migration_factory, tmp_path)
    entered = asyncio.Event()

    class Probe(FakeModelAdapter):
        async def generate(self, request):
            async with migration_factory() as observer:
                row = (await observer.execute(select(model_reservations))).mappings().one()
                assert row["state"] == "dispatched"
            entered.set()
            if outcome == "exception":
                raise RuntimeError("simulated provider failure")
            if outcome == "cancel":
                await asyncio.Event().wait()
            return await super().generate(request)

    adapter = Probe(fixed_input_tokens=10, fixed_output_tokens=5)
    async with migration_factory() as outer:
        budget = ModelBudgetService(outer)
        gateway = ModelGatewayService(
            ModelRouter(budget), budget, {"fake": adapter}, accounting_factory=migration_factory
        )
        if outcome == "cancel":
            task = asyncio.create_task(gateway.execute_call(**args))
            async with asyncio.timeout(10):
                await entered.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        elif outcome == "exception":
            with pytest.raises(RuntimeError, match="simulated provider failure"):
                await gateway.execute_call(**args)
        else:
            await gateway.execute_call(**args)
        await outer.execute(
            update(repositories)
            .where(repositories.c.id == args["repository_id"])
            .values(display_name="outer transaction rolled back")
        )
        await outer.rollback()
    async with migration_factory() as observer:
        row = (await observer.execute(select(model_reservations))).mappings().one()
        assert row["state"] == ("settled" if outcome == "success" else "uncertain_hold")
        budget = await ModelBudgetService(observer).get_budget(
            args["org_id"], args["repository_id"]
        )
        assert budget["reserved_micros"] == (0 if outcome == "success" else row["reserved_micros"])
        assert (
            await observer.scalar(select(repositories.c.display_name))
            != "outer transaction rolled back"
        )


async def test_recovery_holds_abandoned_dispatch_without_replay(migration_factory, tmp_path):
    args = await prepared(migration_factory, tmp_path)
    async with migration_factory() as session, session.begin():
        service = ModelBudgetService(session)
        identity = await service.reserve_generation_budget(
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
        await service.commit_dispatch_intent(args["org_id"], identity)
        now = await database_now(session)
        await session.execute(
            update(model_reservations).values(updated_at=now - timedelta(minutes=5))
        )
    async with migration_factory() as session, session.begin():
        recovered = await ModelBudgetService(session).recover_dispatch_intents(
            args["org_id"], args["repository_id"], minimum_age_seconds=60, limit=10
        )
        assert recovered == (identity,)
    async with migration_factory() as session, session.begin():
        service = ModelBudgetService(session)
        assert (
            await service.recover_dispatch_intents(
                args["org_id"], args["repository_id"], minimum_age_seconds=60, limit=10
            )
            == ()
        )
        assert (await service.get_budget(args["org_id"], args["repository_id"]))[
            "reserved_micros"
        ] == 100


async def test_revoked_writer_cannot_dispatch(migration_factory, tmp_path):
    from app.domain.errors import ScopeViolation
    from app.models.phase2_tables import PHASE2_TABLES

    args = await prepared(migration_factory, tmp_path)
    grants = PHASE2_TABLES["repository_grants"]
    async with migration_factory() as session, session.begin():
        await session.execute(update(grants).values(revoked_at=await database_now(session)))
    adapter = FakeModelAdapter()
    async with migration_factory() as anchor:
        budget = ModelBudgetService(anchor)
        gateway = ModelGatewayService(
            ModelRouter(budget), budget, {"fake": adapter}, accounting_factory=migration_factory
        )
        with pytest.raises(ScopeViolation):
            await gateway.execute_call(**args)
    assert adapter.call_history == []
    async with migration_factory() as observer:
        assert await observer.scalar(select(model_reservations.c.reservation_id)) is None


async def test_http_investigation_uses_independent_accounting(migration_factory, tmp_path):
    import httpx
    from app.api.application import ApiSettings, ApplicationPorts, create_app

    ctx, _snapshot, repo, store = await admitted(migration_factory, tmp_path)
    async with migration_factory() as session, session.begin():
        service = ModelBudgetService(session)
        await service.ensure_default_generation_pricing()
        await service.ensure_budget(ctx.org_id, repo, initial_balance_micros=10_000_000)
    adapter = FakeModelAdapter(
        default_content=(
            '{"verdict":"benign","confidence":0.9,"summary":"Synthetic verdict","claims":[]}'
        ),
        fixed_input_tokens=10,
        fixed_output_tokens=5,
    )
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(migration_factory, store, ctx, model_adapter=adapter),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session = await client.get("/api/v1/session")
        headers = {
            "origin": "http://testserver",
            "x-csrf-token": session.json()["csrf_token"],
            "idempotency-key": "http-durable-investigation",
        }
        created = await client.post(
            f"/api/v1/repositories/{repo}/investigations",
            headers=headers,
            json={"repository_id": str(repo), "query": "Inspect this synthetic fixture"},
        )
        assert created.status_code == 201, created.text
        async with asyncio.timeout(15):
            result = await client.post(
                f"/api/v1/repositories/{repo}/investigations/{created.json()['id']}/execute",
                headers=headers,
            )
        assert result.status_code == 200, result.text
        assert result.json()["summary"]["status"] == "completed"
    async with migration_factory() as observer:
        row = (await observer.execute(select(model_reservations))).mappings().one()
        assert row["state"] == "settled"


async def test_price_and_settlement_identity_across_transactions(migration_factory, tmp_path):
    from app.services.model_budget import ModelInvalidStateTransitionError

    args = await prepared(migration_factory, tmp_path)
    adapter = FakeModelAdapter(fixed_input_tokens=10, fixed_output_tokens=5)
    async with migration_factory() as anchor:
        budget = ModelBudgetService(anchor)
        gateway = ModelGatewayService(
            ModelRouter(budget), budget, {"fake": adapter}, accounting_factory=migration_factory
        )
        response = await gateway.execute_call(**{**args, "max_tokens": 3000})
    async with migration_factory() as session, session.begin():
        service = ModelBudgetService(session)
        row = (await session.execute(select(model_reservations))).mappings().one()
        assert row["estimated_output_tokens"] == 3000
        assert adapter.call_history[0].max_tokens == 3000
        price = await service.get_generation_price_schedule("fake", "codespy-fake-investigator-v1")
        with pytest.raises(ModelInvalidStateTransitionError):
            await service.set_generation_price_schedule(
                price.model_copy(update={"output_micros_per_1k": 9999})
            )
        usage = {
            "org_id": args["org_id"],
            "reservation_id": row["reservation_id"],
            "actual_input_tokens": 10,
            "actual_output_tokens": 5,
            "actual_thinking_tokens": 0,
            "actual_micros": response.charge_micros,
            "provider_request_id": response.provider_request_id,
        }
        await service.settle_reservation(**usage)
        with pytest.raises(ModelInvalidStateTransitionError):
            await service.settle_reservation(
                **{**usage, "actual_micros": response.charge_micros + 1}
            )
