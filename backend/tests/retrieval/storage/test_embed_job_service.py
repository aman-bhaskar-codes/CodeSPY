"""JobService handles embed jobs: enqueue, claim, retry, cancel and authority-loss recovery."""

import json
import os

import pytest
from app.contracts.embedding_jobs import EmbedJobRecordV7, EnqueueEmbedJobV7, JobEnvelopeV7
from app.contracts.jobs import AttemptLeaseV2
from app.domain.errors import DuplicateConflict
from app.models.phase2_tables import PHASE2_TABLES
from app.services.jobs import JobService
from sqlalchemy import func, text, update

from tests.retrieval.storage.test_dense_guards import embed_base

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0046)",
)


def enqueue_request(base, key="embed-1", **overrides):
    values = {
        "repository_id": base["spec"].repository_id,
        "snapshot_id": base["spec"].snapshot_id,
        "embed_spec_id": base["spec_id"],
        "config_hash": base["config_hash"],
        "request_key": key,
    }
    return EnqueueEmbedJobV7(**{**values, **overrides})


async def queued(base, api_factory, key="embed-1"):
    record = await JobService(api_factory).enqueue(base["ctx"], enqueue_request(base, key))
    assert isinstance(record, EmbedJobRecordV7)
    return record


async def rows(migration_factory, query, **params):
    async with migration_factory() as session:
        return (await session.execute(text(query), params)).mappings().all()


async def test_enqueue_is_idempotent_scoped_and_emits_a_v7_wakeup(
    api_factory, broker_factory, migration_factory, tmp_path
):
    base = await embed_base(api_factory, broker_factory, migration_factory, tmp_path)
    record = await queued(base, api_factory)
    assert record.state == "queued" and record.embed_spec_id == base["spec_id"]
    assert (await queued(base, api_factory)).id == record.id  # same key and input: replay
    with pytest.raises(DuplicateConflict):  # same key, different input
        await JobService(api_factory).enqueue(
            base["ctx"], enqueue_request(base, config_hash="f" * 64)
        )
    outbox = await rows(
        migration_factory, "SELECT payload FROM outbox WHERE job_id=:j", j=record.id
    )
    assert len(outbox) == 1
    payload = outbox[0]["payload"]
    envelope = JobEnvelopeV7.model_validate_json(
        json.dumps(json.loads(payload) if isinstance(payload, str) else payload)
    )
    assert envelope.embed_spec_id == base["spec_id"] and envelope.dispatch_generation == 1


async def test_claim_binds_the_attempt_and_transient_failure_retries_with_a_new_fence(
    api_factory, broker_factory, migration_factory, tmp_path
):
    base = await embed_base(api_factory, broker_factory, migration_factory, tmp_path)
    record = await queued(base, api_factory)
    jobs = JobService(broker_factory)
    lease = await jobs.claim(base["ctx"].org_id, record.id, 1, "embed-worker")
    assert isinstance(lease, AttemptLeaseV2) and lease.fence == 1
    bound = await rows(
        migration_factory,
        "SELECT spec_id,spec_hash FROM embed_attempt_specs WHERE id=:a",
        a=lease.attempt_id,
    )
    assert bound[0]["spec_id"] == base["spec_id"] and bound[0]["spec_hash"] == base["spec_hash"]
    assert await jobs.claim(base["ctx"].org_id, record.id, 1, "other") == "already_leased"

    await jobs.fail_attempt(lease, "embed_provider_timeout", transient=True)
    state = await rows(
        migration_factory, "SELECT state,dispatch_generation FROM jobs WHERE id=:j", j=record.id
    )
    assert state[0]["state"] == "retry_wait" and state[0]["dispatch_generation"] == 2
    wakeups = await rows(
        migration_factory,
        "SELECT generation FROM outbox WHERE job_id=:j ORDER BY generation",
        j=record.id,
    )
    assert [w["generation"] for w in wakeups] == [1, 2]


async def test_failure_codes_are_scoped_to_the_operation(
    api_factory, broker_factory, migration_factory, tmp_path
):
    base = await embed_base(api_factory, broker_factory, migration_factory, tmp_path)
    record = await queued(base, api_factory)
    jobs = JobService(broker_factory)
    lease = await jobs.claim(base["ctx"].org_id, record.id, 1, "embed-worker")
    assert isinstance(lease, AttemptLeaseV2)
    for code in ("index_timeout", "investigation_failed", "definitely_unknown"):
        with pytest.raises(ValueError, match="unknown failure code"):
            await jobs.fail_attempt(lease, code)
    await jobs.fail_attempt(lease, "embed_hold_pending", transient=False)
    final = await rows(
        migration_factory, "SELECT state,failure_code FROM jobs WHERE id=:j", j=record.id
    )
    assert (final[0]["state"], final[0]["failure_code"]) == ("failed", "embed_hold_pending")


async def test_running_cancellation_is_terminalized_by_recovery(
    api_factory, broker_factory, migration_factory, tmp_path
):
    base = await embed_base(api_factory, broker_factory, migration_factory, tmp_path)
    ctx = base["ctx"]
    job = await queued(base, api_factory, "cancel-me")
    jobs = JobService(broker_factory)
    lease = await jobs.claim(ctx.org_id, job.id, 1, "embed-worker")
    assert isinstance(lease, AttemptLeaseV2)
    cancelled = await JobService(api_factory).request_cancel(ctx, job.id)
    assert cancelled.state == "cancelling"
    await jobs.recover_due()
    state = await rows(migration_factory, "SELECT state FROM jobs WHERE id=:j", j=job.id)
    assert state[0]["state"] == "cancelled"


async def test_revoked_writer_is_terminalized_by_recovery(
    api_factory, broker_factory, migration_factory, tmp_path
):
    base = await embed_base(api_factory, broker_factory, migration_factory, tmp_path)
    ctx = base["ctx"]
    job = await queued(base, api_factory, "revoked-writer")
    grants = PHASE2_TABLES["repository_grants"]
    async with migration_factory() as session, session.begin():
        await session.execute(
            update(grants)
            .where(grants.c.org_id == ctx.org_id, grants.c.principal_id == ctx.principal_id)
            .values(revoked_at=func.clock_timestamp())
        )
    await JobService(broker_factory).recover_due()
    final = await rows(
        migration_factory, "SELECT state,failure_code FROM jobs WHERE id=:j", j=job.id
    )
    assert (final[0]["state"], final[0]["failure_code"]) == ("failed", "access_revoked")
