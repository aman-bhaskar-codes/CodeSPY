import asyncio
from uuid import uuid4

import pytest
from app.db.transaction import transaction
from app.domain.errors import InvalidTransition, ScopeViolation, StaleWrite
from app.models.tables import analysis_runs, cpg_nodes, source_files, source_snapshots
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError
from tests.support.fixtures import batch_for, scenario


async def wait_for_lock(engine, pid):
    # Observe a PostgreSQL lock wait rather than relying on a sleep to create a race.
    async with engine.connect() as observer:
        for _ in range(200):
            waiting = await observer.scalar(
                text("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"),
                {"pid": pid},
            )
            if waiting:
                return
            await asyncio.sleep(0.005)
    raise AssertionError("second connection never reached expected lock barrier")


async def test_child_writer_vs_cancellation(factory, engine):
    svc, ctx, snap, run, files = await scenario(factory)
    batch = batch_for(run.ref, files)
    async with factory() as writer, writer.begin():
        await writer.execute(
            select(analysis_runs).where(analysis_runs.c.id == run.id).with_for_update()
        )
        ready = asyncio.Event()
        pid = []

        async def cancel():
            async with engine.connect() as conn:
                pid.append(await conn.scalar(text("SELECT pg_backend_pid()")))
                await conn.rollback()
                ready.set()
                async with conn.begin():
                    await conn.execute(
                        update(analysis_runs)
                        .where(analysis_runs.c.id == run.id)
                        .values(
                            status="cancelled",
                            finished_at=text("CURRENT_TIMESTAMP"),
                            write_version=2,
                        )
                    )

        task = asyncio.create_task(cancel())
        await ready.wait()
        await wait_for_lock(engine, pid[0])
        n = batch.nodes[0]
        await writer.execute(
            insert(cpg_nodes).values(
                org_id=ctx.org_id,
                snapshot_id=snap.id,
                run_id=run.id,
                **n.model_dump(exclude={"range"}),
                **n.range.model_dump(),
            )
        )
    await task
    async with transaction(factory) as s:
        assert (
            await s.scalar(select(analysis_runs.c.status).where(analysis_runs.c.id == run.id))
            == "cancelled"
        )
        assert (
            await s.scalar(text("SELECT count(*) FROM cpg_nodes WHERE run_id=:id"), {"id": run.id})
            == 1
        )
    # A subsequent child insert cannot mutate a terminal run.
    with pytest.raises((ScopeViolation, InvalidTransition, StaleWrite)):
        await svc.persist_result(ctx, run.ref, batch, 1)


async def test_terminal_first_rejects_waiting_writer(factory, engine):
    _svc, ctx, snap, run, files = await scenario(factory)
    b = batch_for(run.ref, files)
    ready = asyncio.Event()
    pid = []
    async with engine.begin() as terminal:
        await terminal.execute(
            update(analysis_runs)
            .where(analysis_runs.c.id == run.id)
            .values(status="cancelled", finished_at=text("CURRENT_TIMESTAMP"), write_version=2)
        )

        async def writer():
            async with engine.connect() as c:
                pid.append(await c.scalar(text("SELECT pg_backend_pid()")))
                await c.rollback()
                ready.set()
                with pytest.raises(DBAPIError):
                    async with c.begin():
                        n = b.nodes[0]
                        await c.execute(
                            insert(cpg_nodes).values(
                                org_id=ctx.org_id,
                                snapshot_id=snap.id,
                                run_id=run.id,
                                **n.model_dump(exclude={"range"}),
                                **n.range.model_dump(),
                            )
                        )

        task = asyncio.create_task(writer())
        await ready.wait()
        await wait_for_lock(engine, pid[0])
    await task


async def test_duplicate_publication_serializes(factory):
    svc, ctx, _snap, run, files = await scenario(factory)
    b = batch_for(run.ref, files)
    a, c = await asyncio.gather(
        svc.persist_result(ctx, run.ref, b, 1), svc.persist_result(ctx, run.ref, b, 1)
    )
    assert a == c


async def test_snapshot_seal_vs_file_insert(factory, engine):
    _svc, ctx, snap, _run, _files = await scenario(factory)
    # Sealed parent is locked by one writer; waiting inserts must inspect committed sealed state.
    ready = asyncio.Event()
    pid = []
    async with engine.begin() as holder:
        await holder.execute(
            select(source_snapshots).where(source_snapshots.c.id == snap.id).with_for_update()
        )

        async def late_file():
            async with engine.connect() as c:
                pid.append(await c.scalar(text("SELECT pg_backend_pid()")))
                await c.rollback()
                ready.set()
                with pytest.raises(DBAPIError):
                    async with c.begin():
                        await c.execute(
                            insert(source_files).values(
                                org_id=ctx.org_id,
                                snapshot_id=snap.id,
                                id=uuid4(),
                                path="late.py",
                                sha256="a" * 64,
                                size_bytes=0,
                            )
                        )

        task = asyncio.create_task(late_file())
        await ready.wait()
        await wait_for_lock(engine, pid[0])
    await task
