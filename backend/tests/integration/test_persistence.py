from uuid import uuid4

import pytest
from app.db.health import inspect_database
from app.db.transaction import transaction
from app.domain.errors import (
    DuplicateConflict,
    InvalidTransition,
    NotPublished,
    ScopeViolation,
    StaleWrite,
)
from sqlalchemy import text
from tests.support.fixtures import batch_for, scenario


async def test_database_health(engine):
    caps = await inspect_database(engine)
    assert caps.ready and caps.server_version.startswith("16.")
    async with engine.connect() as c:
        assert await c.scalar(
            text("SELECT '[1,0,0]'::vector <=> '[0,1,0]'::vector")
        ) == pytest.approx(1)


async def test_publish_replay_and_rollback(factory):
    service, ctx, _snapshot, run, file_ids = await scenario(factory)
    ref = run.ref
    with pytest.raises(NotPublished):
        await service.read_published_graph(ctx, ref)
    batch = batch_for(ref, file_ids)
    result = await service.persist_result(ctx, ref, batch, 1)
    assert result.status == "completed" and result.write_version == 2
    assert await service.persist_result(ctx, ref, batch, 1) == result
    graph = await service.read_published_graph(ctx, ref)
    assert len(graph.nodes) == 2 and len(graph.edges) == 1
    changed = batch.model_copy(update={"limitations": ["changed"]})
    with pytest.raises(DuplicateConflict):
        await service.persist_result(ctx, ref, changed, 1)
    run2 = await service.create_run(
        ctx, run.request.model_copy(update={"request_key": uuid4().hex})
    )
    await service.start_run(ctx, run2.ref, 0)
    bad = batch_for(run2.ref, file_ids)
    bad = bad.model_copy(
        update={"edges": [bad.edges[0].model_copy(update={"target_local_id": uuid4()})]}
    )
    with pytest.raises((ScopeViolation, InvalidTransition, StaleWrite)):
        await service.persist_result(ctx, run2.ref, bad, 1)
    async with transaction(factory) as s:
        assert (
            await s.scalar(text("SELECT count(*) FROM cpg_nodes WHERE run_id=:id"), {"id": run2.id})
            == 0
        )
        assert (
            await s.scalar(text("SELECT status FROM analysis_runs WHERE id=:id"), {"id": run2.id})
            == "running"
        )


async def test_stale_cancel(factory):
    service, ctx, _snapshot, run, files = await scenario(factory)
    cancelled = await service.cancel_run(ctx, run.ref, 1)
    assert cancelled.status == "cancelled"
    with pytest.raises((InvalidTransition, StaleWrite)):
        await service.persist_result(ctx, run.ref, batch_for(run.ref, files), 1)


async def test_edgeless_graph_cursor_reaches_every_node(factory):
    svc, ctx, _snap, run, files = await scenario(factory)
    b = batch_for(run.ref, files).model_copy(update={"edges": [], "derivations": []})
    await svc.persist_result(ctx, run.ref, b, 1)
    page = await svc.read_published_graph(ctx, run.ref, limit=1)
    assert page.truncated and page.next_cursor
    second = await svc.read_published_graph(ctx, run.ref, limit=1, after=page.next_cursor)
    assert len({n.local_id for n in page.nodes + second.nodes}) == 2
    assert not second.truncated
