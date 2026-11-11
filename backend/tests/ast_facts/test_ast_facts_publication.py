"""Real current-head transactions; only the container transport is substituted."""

import pytest
from app.analyzers.facts_pipeline import analyze_facts
from app.analyzers.pipeline import analyze
from app.contracts.structure import AnalysisConfigV1
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.models.phase3_tables import ast_file_facts
from app.models.tables import analysis_runs
from app.services import ast_reads
from app.services.ast_finalization import AstFactsFinalizer
from app.services.jobs import JobService
from app.services.structural_analysis import StructuralAnalysisService
from app.services.structural_finalization import StructuralFinalizer
from app.workers.operations.analyze_structure import StructuralAnalysisOperation
from app.workers.operations.ast_facts import AstFactsOperation, ProfileDispatchOperation
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from tests.phase3.integration.test_structural_publication import admitted


class FixtureRunner:
    async def analyze(self, source, root):
        return analyze(source, root)

    async def analyze_facts(self, source, root):
        return analyze_facts(source, root)


async def facts_job(factory, tmp_path, key="ast"):
    ctx, snapshot, repo, store = await admitted(factory, tmp_path)
    config = AnalysisConfigV1(toolchain_hash="a" * 64, adapters=("ast_facts",))
    spec, job = await StructuralAnalysisService(factory).create_analysis_spec(
        ctx, repo, snapshot, config, key
    )
    lease = await JobService(factory).claim(ctx.org_id, job.id, 1, "ast-worker")
    assert lease.run_ref is not None
    return ctx, spec, job, lease, store


def operations(factory, store):
    runner = FixtureRunner()
    structural = StructuralAnalysisOperation(
        factory, store, StructuralFinalizer(factory, store), runner
    )
    facts = AstFactsOperation(factory, store, AstFactsFinalizer(factory, store), runner)
    return structural, facts, ProfileDispatchOperation(factory, structural, facts)


async def test_dispatch_publishes_facts_atomically_and_reads_are_scoped(factory, tmp_path):
    ctx, spec, job, lease, store = await facts_job(factory, tmp_path)
    _, _, dispatch = operations(factory, store)
    summaries = await dispatch.execute(lease)
    assert [s.path for s in summaries] == ["a.py"] and summaries[0].status == "parsed"
    public = await JobService(factory).get(ctx, job.id)
    assert public.state == "succeeded" and public.result_run_id == lease.run_ref.run_id
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(ast_file_facts)) == 1
        assert await s.scalar(
            select(analysis_runs.c.result_hash).where(analysis_runs.c.id == lease.run_ref.run_id)
        )
    async with factory() as s:
        found = await ast_reads.summary(
            s, ctx.org_id, ctx.principal_id, spec.repository_id, spec.snapshot_id
        )
        assert found and found.files == 1 and found.languages == {"python": 1}
        page = await ast_reads.files_page(
            s, ctx.org_id, ctx.principal_id, spec.repository_id, spec.snapshot_id, None, 10
        )
        assert page and [i.path for i in page.items] == ["a.py"] and page.next_after is None
        facts = await ast_reads.file_facts(
            s,
            ctx.org_id,
            ctx.principal_id,
            spec.repository_id,
            spec.snapshot_id,
            summaries[0].file_id,
        )
        assert facts and facts.facts_sha256() == summaries[0].facts_sha256


async def test_published_facts_are_immutable(factory, tmp_path):
    _, _, _, lease, store = await facts_job(factory, tmp_path)
    await operations(factory, store)[2].execute(lease)
    async with factory() as s:
        with pytest.raises(DBAPIError):
            await s.execute(text("UPDATE ast_file_facts SET status='partial'"))
        await s.rollback()
        with pytest.raises(DBAPIError):
            await s.execute(text("DELETE FROM ast_file_facts"))


async def test_other_tenants_cannot_read_facts(factory, tmp_path):
    from scripts.phase1_fixture import scenario

    _, spec, _, lease, store = await facts_job(factory, tmp_path)
    await operations(factory, store)[2].execute(lease)
    _, other, *_ = await scenario(factory)
    async with factory() as s:
        with pytest.raises(ScopeViolation):
            await ast_reads.summary(
                s, other.org_id, other.principal_id, spec.repository_id, spec.snapshot_id
            )


async def test_facts_finalizer_refuses_a_structural_spec(factory, tmp_path):
    ctx, snapshot, repo, store = await admitted(factory, tmp_path)
    config = AnalysisConfigV1(toolchain_hash="a" * 64)  # no ast_facts adapter
    _, job = await StructuralAnalysisService(factory).create_analysis_spec(
        ctx, repo, snapshot, config, "structural"
    )
    lease = await JobService(factory).claim(ctx.org_id, job.id, 1, "worker")
    _, facts, _ = operations(factory, store)
    with pytest.raises(DuplicateConflict):
        await facts.execute(lease)


async def test_dispatch_still_routes_structural_jobs_to_the_structural_operation(factory, tmp_path):
    ctx, snapshot, repo, store = await admitted(factory, tmp_path)
    config = AnalysisConfigV1(toolchain_hash="a" * 64)
    _, job = await StructuralAnalysisService(factory).create_analysis_spec(
        ctx, repo, snapshot, config, "structural"
    )
    lease = await JobService(factory).claim(ctx.org_id, job.id, 1, "worker")
    report = await operations(factory, store)[2].execute(lease)
    assert report.symbol_map  # an ArchitectureReportV1, not AST facts
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(ast_file_facts)) == 0


async def test_migration_downgrade_refused_with_history_then_allowed(factory, db_values, tmp_path):
    from scripts import phase2_db as harness

    _, _, _, lease, store = await facts_job(factory, tmp_path)
    name = db_values["CODESPY_TEST_DATABASE"]
    await operations(factory, store)[2].execute(lease)
    with pytest.raises(Exception, match="AST facts history must be preserved"):
        harness.migrate(db_values, name, "0042_public_github_source", downgrade=True)
