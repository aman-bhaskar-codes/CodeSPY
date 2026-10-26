"""Investigation publication through real disposable PostgreSQL guards."""

import pytest
from app.contracts.investigation_jobs import InvestigationCreateRequestV2
from app.domain.errors import ScopeViolation
from app.models.investigation_execution_tables import investigation_reports
from app.services.investigation_jobs import InvestigationJobService
from app.services.jobs import JobService
from app.workers.investigation_finalizer import InvestigationFinalizer
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from tests.investigations.storage.test_publication import _make_draft
from tests.retrieval.storage.test_index_guards import source_case


@pytest.mark.parametrize("mode", ["success", "expired", "timeout", "orphan"])
async def test_publication_uses_real_postgres_guards(
    migration_factory, broker_factory, tmp_path, monkeypatch, mode
):
    ctx, source, _ = await source_case(migration_factory, tmp_path)
    commands = InvestigationJobService(migration_factory)
    created = await commands.create(
        ctx,
        InvestigationCreateRequestV2(
            repository_id=source.repository_id,
            snapshot_id=source.snapshot_id,
            query="Inspect retained source",
            target_type="repository",
        ),
        request_key="publication-create",
    )
    started = await commands.start(ctx, created.id, request_key="publication-start")
    lease = await JobService(migration_factory).claim(
        ctx.org_id, started.job_id, 1, "publication-test"
    )
    assert lease is not None
    finalizer = InvestigationFinalizer(
        broker_factory, budget_seconds=0.2 if mode == "timeout" else 10
    )
    draft = _make_draft(created.id, created.spec_hash)
    if mode == "expired":
        async with migration_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE public.job_attempts "
                    "SET lease_expires_at=clock_timestamp()-interval '1 second' "
                    "WHERE org_id=:org AND id=:attempt"
                ),
                {"org": ctx.org_id, "attempt": lease.attempt_id},
            )
        with pytest.raises(ScopeViolation):
            await finalizer.publish(ctx, lease, draft)
    elif mode == "timeout":
        original = finalizer._atomic_publish

        async def slow(session, current, prepared):
            result = await original(session, current, prepared)
            for _ in range(5):
                await session.execute(text("SELECT pg_sleep(0.06)"))
            return result

        monkeypatch.setattr(finalizer, "_atomic_publish", slow)
        with pytest.raises(TimeoutError):
            await finalizer.publish(ctx, lease, draft)
    elif mode == "orphan":
        from uuid import uuid4

        from app.repositories.job_repository import worker_context
        from sqlalchemy import insert

        with pytest.raises(DBAPIError):
            async with broker_factory() as session, session.begin():
                await worker_context(session, lease)
                await session.execute(
                    insert(investigation_reports).values(
                        org_id=ctx.org_id,
                        id=uuid4(),
                        investigation_id=created.id,
                        spec_hash=created.spec_hash,
                        job_id=lease.job_id,
                        verdict="indeterminate",
                        verdict_confidence=0,
                        summary="Fixture",
                        report_json={},
                        report_digest="a" * 64,
                        claims_count=0,
                        evidence_refs_count=0,
                    )
                )
    else:
        receipt = await finalizer.publish(ctx, lease, draft)
        assert receipt.job_id == started.job_id
        assert await finalizer.publish(ctx, lease, draft) == receipt
        return
    async with broker_factory() as session:
        assert (await session.execute(select(investigation_reports))).first() is None
