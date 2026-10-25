"""Real PostgreSQL reservation serialization; no provider calls."""

import asyncio
from uuid import uuid4

from app.services.model_budget import ModelBudgetService
from tests.phase3.integration.test_structural_publication import admitted


async def setup(factory, tmp_path):
    ctx, _snapshot, repo, _store = await admitted(factory, tmp_path)
    args = {
        "org_id": ctx.org_id,
        "repository_id": repo,
        "principal_id": ctx.principal_id,
        "investigation_id": uuid4(),
        "logical_call_id": uuid4(),
        "attempt_id": 1,
        "provider": "fake",
        "model_id": "offline",
        "estimated_input_tokens": 10,
        "estimated_output_tokens": 20,
        "estimated_micros": 100,
        "idempotency_key": "same-call",
    }
    async with factory() as session, session.begin():
        await ModelBudgetService(session).ensure_budget(ctx.org_id, repo, 1000)
    return args


async def test_concurrent_identical_reservation_holds_capacity_once(migration_factory, tmp_path):
    args = await setup(migration_factory, tmp_path)
    ready = asyncio.Barrier(2)

    async def reserve():
        async with migration_factory() as session, session.begin():
            await ready.wait()
            return await ModelBudgetService(session).reserve_generation_budget(**args)

    async with asyncio.timeout(10):
        first, second = await asyncio.gather(reserve(), reserve())
    assert first == second
    async with migration_factory() as session:
        budget = await ModelBudgetService(session).get_budget(args["org_id"], args["repository_id"])
        assert budget["reserved_micros"] == 100


async def test_concurrent_release_preserves_other_reservation(migration_factory, tmp_path):
    args = await setup(migration_factory, tmp_path)
    async with migration_factory() as session, session.begin():
        service = ModelBudgetService(session)
        identity = await service.reserve_generation_budget(**args)
        await service.reserve_generation_budget(
            **{**args, "idempotency_key": "other-call", "logical_call_id": uuid4()}
        )
    ready = asyncio.Barrier(2)

    async def release():
        async with migration_factory() as session, session.begin():
            await ready.wait()
            await ModelBudgetService(session).release_reservation(args["org_id"], identity)

    async with asyncio.timeout(10):
        await asyncio.gather(release(), release())
    async with migration_factory() as session:
        budget = await ModelBudgetService(session).get_budget(args["org_id"], args["repository_id"])
        assert budget["reserved_micros"] == 100
