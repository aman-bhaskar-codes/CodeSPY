"""Recovery locks the snapshot without the actor check; normal authorization is unchanged."""

import os
from uuid import uuid4

import pytest
from app.contracts.embedding_jobs import EnqueueEmbedJobV7
from app.domain.errors import ScopeViolation
from app.models.phase2_tables import PHASE2_TABLES
from app.security.authorization import lock_scope
from app.services.jobs import JobService
from sqlalchemy import func, text, update
from sqlalchemy.exc import DBAPIError

from tests.retrieval.storage.test_dense_guards import embed_base

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0047)",
)

LOCK = "SELECT public.codespy_lock_snapshot_for_recovery(:org,:repo,:snap)"


def request(base, key):
    return EnqueueEmbedJobV7(
        repository_id=base["spec"].repository_id,
        snapshot_id=base["spec"].snapshot_id,
        embed_spec_id=base["spec_id"],
        config_hash=base["config_hash"],
        request_key=key,
    )


async def revoke(migration_factory, ctx):
    grants = PHASE2_TABLES["repository_grants"]
    async with migration_factory() as session, session.begin():
        await session.execute(
            update(grants)
            .where(grants.c.org_id == ctx.org_id, grants.c.principal_id == ctx.principal_id)
            .values(revoked_at=func.clock_timestamp())
        )


async def test_the_recovery_lock_checks_the_snapshot_not_the_actor(
    api_factory, broker_factory, migration_factory, tmp_path
):
    base = await embed_base(api_factory, broker_factory, migration_factory, tmp_path)
    org, spec = base["ctx"].org_id, base["spec"]
    args = {"org": org, "repo": spec.repository_id, "snap": spec.snapshot_id}
    async with broker_factory() as session, session.begin():
        assert await session.scalar(text(LOCK), args) is True
        for wrong in (
            {**args, "org": uuid4()},
            {**args, "repo": uuid4()},
            {**args, "snap": uuid4()},
        ):
            assert await session.scalar(text(LOCK), wrong) is False
    with pytest.raises(DBAPIError):  # the API role may not execute it
        async with api_factory() as session, session.begin():
            await session.scalar(text(LOCK), args)


async def test_normal_authorization_still_rejects_a_revoked_actor(
    api_factory, broker_factory, migration_factory, tmp_path
):
    base = await embed_base(api_factory, broker_factory, migration_factory, tmp_path)
    ctx, spec = base["ctx"], base["spec"]
    await revoke(migration_factory, ctx)
    with pytest.raises(ScopeViolation):  # access=True keeps the actor check
        async with broker_factory() as session, session.begin():
            await lock_scope(
                session, ctx.org_id, ctx.principal_id, spec.repository_id, spec.snapshot_id
            )
    async with broker_factory() as session, session.begin():  # recovery does not need it
        await lock_scope(
            session,
            ctx.org_id,
            ctx.principal_id,
            spec.repository_id,
            spec.snapshot_id,
            access=False,
        )


async def test_one_unlockable_job_does_not_block_recovery_of_the_rest(
    api_factory, broker_factory, migration_factory, tmp_path, monkeypatch
):
    base = await embed_base(api_factory, broker_factory, migration_factory, tmp_path)
    ctx = base["ctx"]
    first = await JobService(api_factory).enqueue(ctx, request(base, "stuck"))
    second = await JobService(api_factory).enqueue(ctx, request(base, "recoverable"))
    await revoke(migration_factory, ctx)
    real = JobService._locked

    async def locked(self, s, org_id, job_id, **kwargs):
        if job_id == first.id:
            raise ScopeViolation("snapshot unavailable")
        return await real(self, s, org_id, job_id, **kwargs)

    monkeypatch.setattr(JobService, "_locked", locked)
    changed = await JobService(broker_factory).recover_due()
    assert changed >= 1
    async with migration_factory() as session:
        rows = {
            row.id: (row.state, row.failure_code)
            for row in await session.execute(
                text("SELECT id,state,failure_code FROM jobs WHERE id IN (:a,:b)"),
                {"a": first.id, "b": second.id},
            )
        }
    assert rows[second.id] == ("failed", "access_revoked")
    assert rows[first.id][0] == "queued"  # left for a later pass, not terminalized by mistake
