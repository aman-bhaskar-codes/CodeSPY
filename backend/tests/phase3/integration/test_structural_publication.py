"""Real current-head transactions; fixture analysis substitutes only the container transport."""

import asyncio
from uuid import uuid4

import pytest
from app.admission.zip_inventory import inspect_zip_bytes
from app.analyzers.pipeline import analyze
from app.contracts.ingestion import AdmissionPolicyV1
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.structure import AnalysisConfigV1
from app.domain.errors import DuplicateConflict, StaleAttempt
from app.models.phase3_tables import analysis_specs, structural_reports
from app.models.tables import analysis_runs, cpg_nodes, evidence_records
from app.services.job_finalization import JobFinalizer
from app.services.jobs import JobService
from app.services.retries import RetryService
from app.services.structural_analysis import StructuralAnalysisService
from app.services.structural_finalization import StructuralFinalizer
from app.workers.operations.analyze_structure import StructuralAnalysisOperation
from sqlalchemy import func, select, text

from tests.phase2.integration.test_submission_ingestion import chunks, submission_scenario


class FixtureRunner:
    async def analyze(self, source, root):
        return analyze(source, root)


async def admitted(factory, tmp_path):
    svc, ctx, _, sub, body, store = await submission_scenario(factory, tmp_path)
    uploaded = await svc.write_content(ctx, sub.id, chunks(body))
    lease = await JobService(factory).claim(ctx.org_id, uploaded.job_id, 1, "admission")
    assert isinstance(lease, AttemptLeaseV2)
    finalizer = JobFinalizer(factory, store)
    prepared = await finalizer.prepare_admission(
        lease, inspect_zip_bytes(body, AdmissionPolicyV1())
    )
    result = await finalizer.finish_admission(lease, prepared)
    return ctx, result.snapshot_id, sub.repository_id, store


async def structural(factory, tmp_path):
    ctx, snapshot, repo, store = await admitted(factory, tmp_path)
    config = AnalysisConfigV1(toolchain_hash="a" * 64)
    spec, job = await StructuralAnalysisService(factory).create_analysis_spec(
        ctx, repo, snapshot, config, "analysis"
    )
    lease = await JobService(factory).claim(ctx.org_id, job.id, 1, "structural")
    assert isinstance(lease, AttemptLeaseV2) and lease.run_ref
    return ctx, spec, job, lease, store


async def test_concurrent_identical_request_replays_one_spec_and_job(factory, tmp_path):
    ctx, snapshot, repo, _ = await admitted(factory, tmp_path)
    service = StructuralAnalysisService(factory)
    config = AnalysisConfigV1(toolchain_hash="a" * 64)
    first, second = await asyncio.gather(
        *[service.create_analysis_spec(ctx, repo, snapshot, config, "stable") for _ in range(2)]
    )
    assert first == second
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(analysis_specs)) == 1
    with pytest.raises(DuplicateConflict):
        await service.create_analysis_spec(
            ctx,
            repo,
            snapshot,
            config.model_copy(update={"include_prefixes": ("other",)}),
            "stable",
        )


async def test_worker_publishes_report_graph_and_evidence_atomically(factory, tmp_path):
    ctx, spec, job, lease, store = await structural(factory, tmp_path)
    finalizer = StructuralFinalizer(factory, store)
    operation = StructuralAnalysisOperation(factory, store, finalizer, FixtureRunner())
    report = await operation.execute(lease)
    assert report.symbol_map and report.evidence_map
    public = await JobService(factory).get(ctx, job.id)
    assert public.report_available and public.result_run_id == lease.run_ref.run_id
    async with factory() as s:
        assert await s.scalar(
            select(func.count())
            .select_from(cpg_nodes)
            .where(cpg_nodes.c.run_id == public.result_run_id)
        ) == len(report.symbols)
        assert await s.scalar(
            select(func.count())
            .select_from(evidence_records)
            .where(evidence_records.c.run_id == public.result_run_id)
        ) == len(report.evidence_map)
        assert await s.scalar(
            select(analysis_runs.c.result_hash).where(analysis_runs.c.id == public.result_run_id)
        )
    stored = await StructuralAnalysisService(factory).get_report_by_snapshot(
        ctx, spec.repository_id, spec.snapshot_id
    )
    assert stored == report


async def test_failure_after_report_insert_rolls_back_everything(factory, tmp_path, monkeypatch):
    _, _, _job, lease, store = await structural(factory, tmp_path)
    finalizer = StructuralFinalizer(factory, store)

    async def fail(*args):
        raise RuntimeError("injected failure after report insert")

    monkeypatch.setattr(finalizer.jobs.persistence, "_persist_result_in_session", fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        await StructuralAnalysisOperation(factory, store, finalizer, FixtureRunner()).execute(lease)
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(structural_reports)) == 0
        assert await s.scalar(select(func.count()).select_from(cpg_nodes)) == 0
        assert (
            await s.scalar(
                select(analysis_runs.c.status).where(analysis_runs.c.id == lease.run_ref.run_id)
            )
            == "running"
        )


async def test_lost_commit_response_resolves_both_hashes(factory, tmp_path):
    _, _, _, lease, store = await structural(factory, tmp_path)
    finalizer = StructuralFinalizer(factory, store)

    async def lost(s):
        await s.commit()
        raise ConnectionError("lost commit response")

    finalizer.executor.commit_hook = lost
    report = await StructuralAnalysisOperation(factory, store, finalizer, FixtureRunner()).execute(
        lease
    )
    assert report.run_ref == lease.run_ref


async def test_expired_database_lease_rejects_stale_publication(factory, tmp_path):
    _, _, _, lease, store = await structural(factory, tmp_path)

    async def expire():
        async with factory() as s, s.begin():
            await s.execute(
                text(
                    "UPDATE job_attempts SET "
                    "lease_expires_at=clock_timestamp()-interval '1 second' "
                    "WHERE id=:id"
                ),
                {"id": lease.attempt_id},
            )

    with pytest.raises(StaleAttempt):
        await StructuralAnalysisOperation(
            factory, store, StructuralFinalizer(factory, store), FixtureRunner()
        ).execute(lease, enter_finalization=expire)
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(structural_reports)) == 0


async def test_structural_cancel_retry_preserves_spec_and_allocates_new_run(factory, tmp_path):
    ctx, spec, job, lease, store = await structural(factory, tmp_path)
    jobs = JobService(factory)
    assert (await jobs.cancel(ctx, job.id)).state == "cancelled"
    retry = await RetryService(factory, store).retry(ctx, job.id, "retry")
    assert retry.analysis_spec_id == spec.id and retry.submission_id is None
    assert await RetryService(factory, store).retry(ctx, job.id, "retry") == retry
    new = await jobs.claim(ctx.org_id, retry.retry_job_id, 1, uuid4().hex)
    assert isinstance(new, AttemptLeaseV2) and new.run_ref != lease.run_ref
    with pytest.raises(StaleAttempt):
        await StructuralAnalysisOperation(
            factory, store, StructuralFinalizer(factory, store), FixtureRunner()
        ).execute(lease)


async def test_scoped_api_summary_sections_graph_evidence_and_export(factory, tmp_path):
    import httpx
    from app.api.application import ApiSettings, ApplicationPorts, create_app
    from app.domain.canonical_json import canonical_bytes
    from app.security.sessions import SessionService

    ctx, spec, _, lease, store = await structural(factory, tmp_path)
    report = await StructuralAnalysisOperation(
        factory, store, StructuralFinalizer(factory, store), FixtureRunner()
    ).execute(lease)
    issued = await SessionService(factory).issue(ctx.principal_id)
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(factory, store, ctx),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies={"codespy_dev": issued.token},
    ) as client:
        base = f"/api/v1/repositories/{spec.repository_id}/analyses/{report.run_ref.run_id}"
        small = await client.get(base + "/report")
        assert small.status_code == 200 and not small.json()["symbols"]
        assert len(small.content) <= 65536
        section = await client.get(base + "/report/symbols?limit=1")
        assert section.status_code == 200 and len(section.json()["items"]) == 1
        cursor = section.json()["next_cursor"]
        assert (
            await client.get(base + "/report/excerpts", params={"after": cursor})
        ).status_code == 422
        graph = await client.get(base + "/graph")
        assert graph.status_code == 200 and graph.json()["nodes"]
        ev = await client.get(base + f"/evidence/{report.evidence_map[0].physical_id}")
        assert ev.status_code == 200 and ev.json()["evidence"]["scope"]["run_id"] == str(
            report.run_ref.run_id
        )
        exported = await client.get(base + "/export?format=json")
        assert exported.content == canonical_bytes(report.model_dump())
        traversal = await client.post(
            base + "/traversal",
            json={"seeds": [graph.json()["nodes"][0]["local_id"]], "edge_kind": "AST_CHILD"},
            headers={"origin": "http://testserver", "x-csrf-token": issued.csrf},
        )
        assert traversal.status_code == 200
        assert (await client.post(base + "/traversal", json={})).status_code == 403
        disabled = await client.post(
            f"/api/v1/repositories/{spec.repository_id}/analyses",
            json={"snapshot_id": str(spec.snapshot_id)},
            headers={
                "origin": "http://testserver",
                "x-csrf-token": issued.csrf,
                "idempotency-key": "disabled",
            },
        )
        assert disabled.status_code == 503 and disabled.json()["code"] == "analysis_unavailable"
        async with factory() as s, s.begin():
            await s.execute(
                text(
                    "UPDATE repository_grants SET revoked_at=clock_timestamp() "
                    "WHERE org_id=:org AND repository_id=:repo"
                ),
                {"org": ctx.org_id, "repo": spec.repository_id},
            )
        assert (await client.get(base + "/report")).status_code == 404
        assert (await client.get(base + "/graph")).status_code == 404


async def test_database_authority_rejects_expiry_after_entry(factory, tmp_path, monkeypatch):
    from sqlalchemy.exc import DBAPIError

    _, _, _, lease, store = await structural(factory, tmp_path)
    finalizer = StructuralFinalizer(factory, store)
    original = finalizer._entry

    async def expire_after_entry(s, bound, job, budget):
        result = await original(s, bound, job, budget)
        await s.execute(
            text(
                "UPDATE job_attempts SET lease_expires_at=clock_timestamp()-interval '1 second' "
                "WHERE id=:id"
            ),
            {"id": lease.attempt_id},
        )
        return result

    monkeypatch.setattr(finalizer, "_entry", expire_after_entry)
    with pytest.raises(DBAPIError, match="stale attempt"):
        await StructuralAnalysisOperation(factory, store, finalizer, FixtureRunner()).execute(lease)
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(structural_reports)) == 0
        assert await s.scalar(select(func.count()).select_from(cpg_nodes)) == 0


async def test_overdue_queued_structural_job_is_recovered_without_queue(factory, tmp_path):
    ctx, snapshot, repo, _store = await admitted(factory, tmp_path)
    _, job = await StructuralAnalysisService(factory).create_analysis_spec(
        ctx, repo, snapshot, AnalysisConfigV1(toolchain_hash="a" * 64), "overdue"
    )
    async with factory() as s, s.begin():
        await s.execute(
            text("UPDATE jobs SET deadline=clock_timestamp()-interval '1 second' WHERE id=:id"),
            {"id": job.id},
        )
    assert await JobService(factory).recover_due() >= 1
    result = await JobService(factory).get(ctx, job.id)
    assert result.state == "failed" and result.failure_code == "deadline_exceeded"
    assert not result.report_available and result.result_run_id is None


async def test_other_writer_must_start_new_spec_instead_of_retry_or_replay(factory, tmp_path):
    from app.domain.errors import RetryRejected, ScopeViolation
    from app.models.phase2_tables import PHASE2_TABLES
    from app.models.tables import memberships, principals
    from sqlalchemy import insert, update

    ctx, spec, job, _, store = await structural(factory, tmp_path)
    await JobService(factory).cancel(ctx, job.id)
    other_id = uuid4()
    other = ctx.model_copy(update={"principal_id": other_id})
    grants = PHASE2_TABLES["repository_grants"]
    async with factory() as s, s.begin():
        await s.execute(
            insert(principals).values(
                id=other_id, subject_key=str(other_id), display_name="Other writer"
            )
        )
        await s.execute(
            insert(memberships).values(
                org_id=ctx.org_id, principal_id=other_id, role=ctx.role.value
            )
        )
        await s.execute(
            insert(grants).values(
                org_id=ctx.org_id,
                principal_id=other_id,
                repository_id=spec.repository_id,
                permission="write",
            )
        )
    service = StructuralAnalysisService(factory)
    with pytest.raises(DuplicateConflict):
        await service.create_analysis_spec(
            other, spec.repository_id, spec.snapshot_id, spec.config, "analysis"
        )
    with pytest.raises(RetryRejected, match="retry_principal_mismatch"):
        await RetryService(factory, store).retry(other, job.id, "other-retry")
    fresh, fresh_job = await service.create_analysis_spec(
        other, spec.repository_id, spec.snapshot_id, spec.config, "other-analysis"
    )
    assert fresh.id != spec.id and fresh.principal_id == other_id
    assert fresh_job.id != job.id
    async with factory() as s, s.begin():
        await s.execute(
            update(grants)
            .where(grants.c.org_id == ctx.org_id, grants.c.principal_id == ctx.principal_id)
            .values(revoked_at=func.clock_timestamp())
        )
    with pytest.raises(ScopeViolation):
        await RetryService(factory, store).retry(ctx, job.id, "revoked-retry")
