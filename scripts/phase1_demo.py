#!/usr/bin/env python3
"""Synthetic persistence demo in an explicitly owned disposable database."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]
from app.core.database_config import DatabaseSettings
from app.db.session import build_engine, build_session_factory
from app.db.transaction import transaction
from app.domain.errors import (
    DuplicateConflict,
    InvalidTransition,
    ScopeViolation,
    StaleWrite,
)
from sqlalchemy import text

from scripts import phase1_db as db
from scripts.phase1_fixture import batch_for, scenario


async def demonstrate(factory):
    svc, ctx, _snap, run, files = await scenario(factory)
    batch = batch_for(run.ref, files)
    published = await svc.persist_result(ctx, run.ref, batch, 1)
    assert await svc.persist_result(ctx, run.ref, batch, 1) == published
    try:
        await svc.persist_result(
            ctx, run.ref, batch.model_copy(update={"limitations": ["different"]}), 1
        )
    except DuplicateConflict:
        pass
    else:
        raise AssertionError("different replay accepted")
    graph = await svc.read_published_graph(ctx, run.ref)
    assert len(graph.nodes) == 2 and len(graph.edges) == 1
    async with transaction(factory) as s:
        counts = {
            t: await s.scalar(
                text(f"SELECT count(*) FROM {t} WHERE run_id=:id"), {"id": run.id}
            )
            for t in ["evidence_records", "evidence_derivations"]
        }
    assert counts == {"evidence_records": 2, "evidence_derivations": 1}
    r2 = await svc.create_run(
        ctx, run.request.model_copy(update={"request_key": uuid4().hex})
    )
    await svc.start_run(ctx, r2.ref, 0)
    bad = batch_for(r2.ref, files)
    bad = bad.model_copy(
        update={"edges": [bad.edges[0].model_copy(update={"target_local_id": uuid4()})]}
    )
    try:
        await svc.persist_result(ctx, r2.ref, bad, 1)
    except ScopeViolation:
        pass
    else:
        raise AssertionError("invalid endpoint accepted")
    async with transaction(factory) as s:
        assert (
            await s.scalar(
                text("SELECT count(*) FROM cpg_nodes WHERE run_id=:id"), {"id": r2.id}
            )
            == 0
        )
        assert (
            await s.scalar(
                text("SELECT count(*) FROM evidence_records WHERE run_id=:id"),
                {"id": r2.id},
            )
            == 0
        )
    _, other_ctx, _, _, _ = await scenario(factory)
    try:
        await svc.read_published_graph(other_ctx, run.ref)
    except ScopeViolation:
        pass
    else:
        raise AssertionError("foreign read accepted")
    await svc.cancel_run(ctx, r2.ref, 1)
    try:
        await svc.persist_result(ctx, r2.ref, batch_for(r2.ref, files), 1)
    except (StaleWrite, InvalidTransition):
        pass
    else:
        raise AssertionError("stale write accepted")
    return {
        "nodes": 2,
        "edges": 1,
        "evidence": 2,
        "derivations": 1,
        "replay": True,
        "rollback": True,
        "scope_rejection": True,
        "stale_rejection": True,
    }


async def execute(values, name):
    engine = build_engine(
        DatabaseSettings(
            DATABASE_URL=db.url(
                values, name, "p1_app", values["CODESPY_TEST_APP_PASSWORD"]
            ).render_as_string(hide_password=False)
        )
    )
    try:
        return await demonstrate(build_session_factory(engine))
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    values = db.read_env(args.env_file)
    db.verify_marker(values, values["CODESPY_TEST_DATABASE"])
    name = db.create_database(values, "demo")
    try:
        db.migrate(values, name)
        db.grant_app(values, name)
        result = asyncio.run(execute(values, name))
    finally:
        db.drop_database(values, name)
    print(json.dumps({"demo": result, "owned_database_removed": True}, sort_keys=True))


if __name__ == "__main__":
    main()
