"""Real PostgreSQL indexing lifecycle; capability readiness is explicitly mocked."""

import asyncio
from uuid import uuid4

import pytest
from app.contracts.jobs import AttemptLeaseV2
from app.domain.errors import CapacityExceeded, DuplicateConflict, RetryRejected, ScopeViolation
from app.domain.scope import OrgRole, TenantContext
from app.models.index_tables import index_attempt_specs, index_jobs, index_specs
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import analysis_runs, memberships, principals
from app.ports.index_builder import IndexBuilderUnavailable
from app.services.jobs import JobService
from sqlalchemy import func, insert, select, text, update
from tests.retrieval.storage.test_index_guards import source_case


class ReadyCapabilities:
    """Synthetic readiness only; native runtime qualification is separate."""

    async def validate(self, config):
        return None


def service(factory):
    from app.services.index_jobs import IndexJobService

    return IndexJobService(factory, ReadyCapabilities())


async def writer(factory, ctx, repo):
    principal = uuid4()
    async with factory() as s, s.begin():
        await s.execute(
            insert(principals).values(
                id=principal, subject_key=str(principal), display_name="Other writer"
            )
        )
        await s.execute(
            insert(memberships).values(org_id=ctx.org_id, principal_id=principal, role="member")
        )
        await s.execute(
            insert(PHASE2_TABLES["repository_grants"]).values(
                org_id=ctx.org_id, repository_id=repo, principal_id=principal, permission="write"
            )
        )
    return TenantContext(org_id=ctx.org_id, principal_id=principal, role=OrgRole.MEMBER)


async def test_create_is_atomic_and_concurrent_key_replay_returns_same_job(
    api_factory, migration_factory, tmp_path
):
    ctx, spec, _ = await source_case(migration_factory, tmp_path)
    svc = service(api_factory)
    a, b = await asyncio.gather(
        *(
            svc.create(ctx, spec.repository_id, spec.snapshot_id, spec.config, "stable")
            for _ in range(2)
        )
    )
    assert a == b and a.job.operation == "index_snapshot"
    async with api_factory() as s:
        assert await s.scalar(select(func.count()).select_from(index_specs)) == 1
        assert (
            await s.scalar(
                select(func.count())
                .select_from(index_jobs)
                .where(index_jobs.c.operation == "index_snapshot")
            )
            == 1
        )
        outbox = PHASE2_TABLES["outbox"]
        events = PHASE2_TABLES["job_events"]
        payload = await s.scalar(select(outbox.c.payload).where(outbox.c.job_id == a.job.id))
        assert payload["schema_version"] == 5 and payload["index_spec_id"] == str(
            a.job.index_spec_id
        )
        assert (
            await s.scalar(
                select(func.count()).select_from(events).where(events.c.job_id == a.job.id)
            )
            == 1
        )
    status = await svc.get(ctx, spec.repository_id, spec.snapshot_id, a.job.id)
    assert status.job == a.job and status.receipt is None


async def test_request_key_cannot_cross_principals_or_config(
    api_factory, migration_factory, tmp_path
):
    ctx, spec, _ = await source_case(migration_factory, tmp_path)
    svc = service(api_factory)
    first = await svc.create(ctx, spec.repository_id, spec.snapshot_id, spec.config, "stable")
    other = await writer(migration_factory, ctx, spec.repository_id)
    with pytest.raises(DuplicateConflict):
        await svc.create(other, spec.repository_id, spec.snapshot_id, spec.config, "stable")
    with pytest.raises(DuplicateConflict):
        await svc.create(
            ctx,
            spec.repository_id,
            spec.snapshot_id,
            spec.config.model_copy(update={"runtime_source_sha256": "b" * 64}),
            "stable",
        )
    second = await svc.create(other, spec.repository_id, spec.snapshot_id, spec.config, "other")
    assert second.job.id != first.job.id


async def test_unavailable_runtime_and_pending_quota_leave_no_orphan_spec(
    api_factory, migration_factory, tmp_path
):
    from app.services.index_jobs import IndexJobService

    ctx, spec, _ = await source_case(migration_factory, tmp_path)

    class Unavailable:
        async def validate(self, config):
            raise IndexBuilderUnavailable("index_builder_unavailable")

    with pytest.raises(IndexBuilderUnavailable):
        await IndexJobService(api_factory, Unavailable()).create(
            ctx, spec.repository_id, spec.snapshot_id, spec.config, "missing"
        )
    limits = PHASE2_TABLES["org_work_limits"]
    async with migration_factory() as s, s.begin():
        await s.execute(
            update(limits).where(limits.c.org_id == ctx.org_id).values(max_pending_jobs=1)
        )
    # Admission already queued a child inventory job, occupying the only pending slot.
    with pytest.raises(CapacityExceeded):
        await service(api_factory).create(
            ctx, spec.repository_id, spec.snapshot_id, spec.config, "quota"
        )
    async with api_factory() as s:
        assert await s.scalar(select(func.count()).select_from(index_specs)) == 0


async def test_claim_binds_attempt_without_analysis_run_or_upload_reservation(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, spec, _ = await source_case(migration_factory, tmp_path)
    created = await service(api_factory).create(
        ctx, spec.repository_id, spec.snapshot_id, spec.config, "claim"
    )
    jobs = JobService(broker_factory)
    async with broker_factory() as s:
        count = await s.scalar(select(func.count()).select_from(analysis_runs))
        retained = {
            name: [dict(row) for row in (await s.execute(select(PHASE2_TABLES[name]))).mappings()]
            for name in ("object_records", "source_submissions")
        }
        limits = dict((await s.execute(select(PHASE2_TABLES["org_work_limits"]))).mappings().one())
    lease = await jobs.claim(ctx.org_id, created.job.id, 1, "index-worker")
    assert isinstance(lease, AttemptLeaseV2) and lease.run_ref is None
    assert await jobs.claim(ctx.org_id, created.job.id, 1, "replacement") == "already_leased"
    await jobs.heartbeat(lease)
    async with broker_factory() as s:
        attempt = (
            (
                await s.execute(
                    select(index_attempt_specs).where(index_attempt_specs.c.id == lease.attempt_id)
                )
            )
            .mappings()
            .one()
        )
        assert attempt["spec_id"] == created.job.index_spec_id and attempt["fence"] == lease.fence
        assert await s.scalar(select(func.count()).select_from(analysis_runs)) == count
        after = dict((await s.execute(select(PHASE2_TABLES["org_work_limits"]))).mappings().one())
        assert after == limits

    await JobService(api_factory).request_cancel(ctx, created.job.id)
    await jobs.recover_due()
    await jobs.recover_due()
    async with broker_factory() as s:
        for name, expected in retained.items():
            actual = [
                dict(row) for row in (await s.execute(select(PHASE2_TABLES[name]))).mappings()
            ]
            assert actual == expected
        after = dict((await s.execute(select(PHASE2_TABLES["org_work_limits"]))).mappings().one())
        assert after == limits
        attempts = PHASE2_TABLES["job_attempts"]
        assert (
            await s.scalar(
                select(func.count())
                .select_from(attempts)
                .where(attempts.c.job_id == created.job.id, attempts.c.state == "active")
            )
            == 0
        )
    assert (
        await service(api_factory).get(ctx, spec.repository_id, spec.snapshot_id, created.job.id)
    ).job.state == "cancelled"


async def test_retry_is_principal_bound_and_immutable_after_cancel(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, spec, _ = await source_case(migration_factory, tmp_path)
    svc = service(api_factory)
    created = await svc.create(ctx, spec.repository_id, spec.snapshot_id, spec.config, "original")
    await JobService(api_factory).request_cancel(ctx, created.job.id)
    other = await writer(migration_factory, ctx, spec.repository_id)
    with pytest.raises(RetryRejected):
        await svc.retry(other, spec.repository_id, created.job.id, "retry")
    a, b = await asyncio.gather(
        *(svc.retry(ctx, spec.repository_id, created.job.id, "retry") for _ in range(2))
    )
    assert a == b and a.retry_job_id != created.job.id and a.retry_of_job_id == created.job.id
    async with api_factory() as s:
        old = (
            (await s.execute(select(index_jobs).where(index_jobs.c.id == created.job.id)))
            .mappings()
            .one()
        )
        new = (
            (await s.execute(select(index_jobs).where(index_jobs.c.id == a.retry_job_id)))
            .mappings()
            .one()
        )
        assert old["state"] == "cancelled" and new["state"] == "queued"
        assert new["attempt_count"] == 0 and new["retry_of_job_id"] == old["id"]
        assert new["index_spec_id"] == old["index_spec_id"] and new["deadline"] > old["deadline"]


async def test_integrity_failure_is_not_manually_retryable(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, spec, _ = await source_case(migration_factory, tmp_path)
    svc = service(api_factory)
    created = await svc.create(ctx, spec.repository_id, spec.snapshot_id, spec.config, "integrity")
    jobs = JobService(broker_factory)
    lease = await jobs.claim(ctx.org_id, created.job.id, 1, "worker")
    await jobs.fail_attempt(lease, "integrity_failure", transient=False)
    with pytest.raises(RetryRejected):
        await svc.retry(ctx, spec.repository_id, created.job.id, "unsupported")


async def test_recovery_terminalizes_expired_queued_job_without_dispatcher(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, spec, _ = await source_case(migration_factory, tmp_path)
    created = await service(api_factory).create(
        ctx, spec.repository_id, spec.snapshot_id, spec.config, "queued-expiry"
    )
    async with migration_factory() as s, s.begin():
        await s.execute(
            text("UPDATE jobs SET deadline=clock_timestamp()-interval '1 second' WHERE id=:id"),
            {"id": created.job.id},
        )
    await JobService(broker_factory).recover_due()
    await JobService(broker_factory).recover_due()
    status = await service(api_factory).get(
        ctx, spec.repository_id, spec.snapshot_id, created.job.id
    )
    assert status.job.state == "failed" and status.job.failure_code == "deadline_exceeded"
    assert status.receipt is None


async def test_revoked_writer_and_running_cancellation_are_terminalized_by_recovery(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, spec, _ = await source_case(migration_factory, tmp_path)
    svc = service(api_factory)
    first = await svc.create(
        ctx, spec.repository_id, spec.snapshot_id, spec.config, "cancel-running"
    )
    jobs = JobService(broker_factory)
    lease = await jobs.claim(ctx.org_id, first.job.id, 1, "worker")
    assert isinstance(lease, AttemptLeaseV2)
    cancelled = await JobService(api_factory).request_cancel(ctx, first.job.id)
    assert cancelled.state == "cancelling"
    await jobs.recover_due()
    assert (
        await svc.get(ctx, spec.repository_id, spec.snapshot_id, first.job.id)
    ).job.state == "cancelled"
    second = await svc.create(ctx, spec.repository_id, spec.snapshot_id, spec.config, "revoked")
    grants = PHASE2_TABLES["repository_grants"]
    async with migration_factory() as s, s.begin():
        await s.execute(
            update(grants)
            .where(grants.c.org_id == ctx.org_id, grants.c.principal_id == ctx.principal_id)
            .values(revoked_at=func.clock_timestamp())
        )
    await jobs.recover_due()
    async with migration_factory() as s:
        row = (
            (await s.execute(select(index_jobs).where(index_jobs.c.id == second.job.id)))
            .mappings()
            .one()
        )
        assert row["state"] == "failed" and row["failure_code"] == "access_revoked"
        attempt = (
            (
                await s.execute(
                    select(PHASE2_TABLES["job_attempts"]).where(
                        PHASE2_TABLES["job_attempts"].c.id == lease.attempt_id
                    )
                )
            )
            .mappings()
            .one()
        )
        assert attempt["state"] == "cancelled"


async def test_lease_replacement_keeps_attempt_history_and_rejects_old_heartbeat(
    api_factory, broker_factory, migration_factory, tmp_path
):
    from app.domain.errors import StaleAttempt

    ctx, spec, _ = await source_case(migration_factory, tmp_path)
    created = await service(api_factory).create(
        ctx, spec.repository_id, spec.snapshot_id, spec.config, "replace"
    )
    jobs = JobService(broker_factory)
    first = await jobs.claim(ctx.org_id, created.job.id, 1, "worker-a")
    async with migration_factory() as s, s.begin():
        await s.execute(
            text(
                "UPDATE job_attempts SET lease_expires_at=clock_timestamp()-interval '1 second' "
                "WHERE id=:id"
            ),
            {"id": first.attempt_id},
        )
    await jobs.recover_due()
    async with migration_factory() as s, s.begin():
        await s.execute(
            text("UPDATE jobs SET not_before=clock_timestamp()-interval '1 second' WHERE id=:id"),
            {"id": created.job.id},
        )
    second = await jobs.claim(ctx.org_id, created.job.id, 2, "worker-b")
    assert (
        isinstance(second, AttemptLeaseV2) and second.fence > first.fence and second.run_ref is None
    )
    with pytest.raises(StaleAttempt):
        await jobs.heartbeat(first)
    async with broker_factory() as s:
        assert (
            await s.scalar(
                select(func.count())
                .select_from(index_attempt_specs)
                .where(index_attempt_specs.c.job_id == created.job.id)
            )
            == 2
        )


async def test_transient_exhaustion_can_retry_but_retry_key_cannot_change_parent(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, spec, _ = await source_case(migration_factory, tmp_path)
    svc = service(api_factory)
    created = await svc.create(ctx, spec.repository_id, spec.snapshot_id, spec.config, "exhaust")
    async with migration_factory() as s, s.begin():
        await s.execute(
            update(index_jobs).where(index_jobs.c.id == created.job.id).values(max_attempts=1)
        )
    jobs = JobService(broker_factory)
    lease = await jobs.claim(ctx.org_id, created.job.id, 1, "worker")
    await jobs.fail_attempt(lease, "index_process_failed", transient=True)
    retried = await svc.retry(ctx, spec.repository_id, created.job.id, "retry")
    status = await svc.get(ctx, spec.repository_id, spec.snapshot_id, created.job.id)
    assert status.job.failure_code == "attempts_exhausted" and status.job.state == "failed"
    assert retried.retry_job_id != created.job.id
    other = await svc.create(ctx, spec.repository_id, spec.snapshot_id, spec.config, "other-parent")
    await JobService(api_factory).request_cancel(ctx, other.job.id)
    with pytest.raises(DuplicateConflict):
        await svc.retry(ctx, spec.repository_id, other.job.id, "retry")


async def test_unwired_consumer_defers_valid_index_envelope_without_claiming(
    api_factory, broker_factory, migration_factory, tmp_path
):
    from app.domain.canonical_json import canonical_bytes
    from app.ports.queue import Delivery
    from app.workers.consumer import JobConsumer

    ctx, spec, _ = await source_case(migration_factory, tmp_path)
    created = await service(api_factory).create(
        ctx, spec.repository_id, spec.snapshot_id, spec.config, "queue"
    )
    async with api_factory() as s:
        payload = await s.scalar(
            select(PHASE2_TABLES["outbox"].c.payload).where(
                PHASE2_TABLES["outbox"].c.job_id == created.job.id
            )
        )

    class Queue:
        def __init__(self):
            self.acked = []
            self.extended = []

        async def ack(self, receipt):
            self.acked.append(receipt)

        async def extend(self, receipt, seconds):
            self.extended.append((receipt, seconds))

    queue = Queue()
    consumer = JobConsumer(broker_factory, queue, None, None)
    await consumer.handle(Delivery(canonical_bytes(payload), "receipt"))
    assert not queue.acked and queue.extended == [("receipt", 120)]
    status = await service(api_factory).get(
        ctx, spec.repository_id, spec.snapshot_id, created.job.id
    )
    assert status.job.state == "queued" and status.job.attempt_count == 0
    forged = {**payload, "index_spec_id": str(uuid4())}
    await consumer.handle(Delivery(canonical_bytes(forged), "forged"))
    assert queue.acked == ["forged"]
    await JobService(api_factory).request_cancel(ctx, created.job.id)
    await consumer.handle(Delivery(canonical_bytes(payload), "terminal"))
    assert queue.acked == ["forged", "terminal"]


async def test_retry_requires_current_write_and_an_unexpired_original_deadline(
    api_factory, migration_factory, tmp_path
):
    ctx, spec, _ = await source_case(migration_factory, tmp_path)
    svc = service(api_factory)
    created = await svc.create(ctx, spec.repository_id, spec.snapshot_id, spec.config, "cancel")
    await JobService(api_factory).request_cancel(ctx, created.job.id)
    grants = PHASE2_TABLES["repository_grants"]
    async with migration_factory() as s, s.begin():
        await s.execute(
            update(grants)
            .where(grants.c.org_id == ctx.org_id, grants.c.principal_id == ctx.principal_id)
            .values(permission="read")
        )
    with pytest.raises(ScopeViolation):
        await svc.retry(ctx, spec.repository_id, created.job.id, "retry")
    async with migration_factory() as s, s.begin():
        await s.execute(
            update(grants)
            .where(grants.c.org_id == ctx.org_id, grants.c.principal_id == ctx.principal_id)
            .values(permission="write")
        )
    expired = await svc.create(ctx, spec.repository_id, spec.snapshot_id, spec.config, "expiring")
    async with migration_factory() as s, s.begin():
        await s.execute(
            text("UPDATE jobs SET deadline=clock_timestamp()-interval '1 second' WHERE id=:id"),
            {"id": expired.job.id},
        )
    await JobService(api_factory).request_cancel(ctx, expired.job.id)
    with pytest.raises(RetryRejected):
        await svc.retry(ctx, spec.repository_id, expired.job.id, "expired")
