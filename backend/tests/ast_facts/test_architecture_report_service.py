# ruff: noqa: E501
"""Architecture reports against real PostgreSQL: shards, build from published facts, immutability."""

import json
import os
from uuid import uuid4

import pytest
from app.contracts.structure import AnalysisConfigV1
from app.domain.errors import CapacityExceeded, ScopeViolation
from app.services.architecture_reports import (
    ArchitectureReportService,
    ReportInputsPending,
    shard_key,
)
from app.services.jobs import JobService
from app.services.structural_analysis import StructuralAnalysisService
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.ast_facts.test_ast_facts_publication import operations
from tests.phase3.integration.test_structural_publication import admitted

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0048)",
)


def config_for(prefixes):
    return AnalysisConfigV1(
        toolchain_hash="a" * 64, include_prefixes=prefixes, adapters=("ast_facts",)
    )


async def setup(factory, tmp_path):
    ctx, snapshot, repo, store = await admitted(factory, tmp_path)
    service = ArchitectureReportService(factory, StructuralAnalysisService(factory))
    return ctx, snapshot, repo, store, service


async def publish_shards(factory, ctx, shards, store):
    for shard in shards.shards:
        lease = await JobService(factory).claim(ctx.org_id, shard.job_id, 1, "report-worker")
        await operations(factory, store)[2].execute(lease)


async def test_prepare_is_idempotent_and_build_waits_for_published_facts(factory, tmp_path):
    ctx, snapshot, repo, store, service = await setup(factory, tmp_path)
    first = await service.prepare(ctx, repo, snapshot, config_for)
    assert len(first.shards) == 1 and first.shards[0].state == "queued" and not first.ready
    assert first.total_files == 1 and first.skipped_files == 0
    again = await service.prepare(ctx, repo, snapshot, config_for)
    assert again.shards[0].job_id == first.shards[0].job_id  # replayed, not duplicated
    with pytest.raises(ReportInputsPending) as pending:
        await service.build(ctx, repo, snapshot)
    assert not pending.value.shards.ready

    await publish_shards(factory, ctx, first, store)
    ready = await service.status(ctx, repo, snapshot)
    assert ready.ready and ready.shards[0].state == "succeeded" and ready.shards[0].run_id


async def test_build_stores_an_immutable_report_once_per_inputs_and_serves_it(factory, tmp_path):
    ctx, snapshot, repo, store, service = await setup(factory, tmp_path)
    shards = await service.prepare(ctx, repo, snapshot, config_for)
    await publish_shards(factory, ctx, shards, store)
    ref = await service.build(ctx, repo, snapshot)
    assert ref.complete and (ref.analyzed_files, ref.eligible_files) == (1, 1)
    assert (await service.build(ctx, repo, snapshot)).id == ref.id  # same inputs: same report
    assert await service.latest(ctx, repo, snapshot) == ref.id
    report = await service.get(ctx, repo, snapshot, ref.id)
    assert report.digest() == ref.report_digest and report.coverage.complete
    assert [n.label for n in report.graph.nodes if n.kind == "file"] == ["a.py"]

    async with factory() as s:
        for statement in (
            "UPDATE architecture_reports SET size_bytes=3",
            "DELETE FROM architecture_reports",
        ):
            with pytest.raises(DBAPIError):
                await s.execute(text(statement))
            await s.rollback()


async def test_the_database_refuses_reports_that_cite_missing_or_altered_inputs(factory, tmp_path):
    ctx, snapshot, repo, store, service = await setup(factory, tmp_path)
    shards = await service.prepare(ctx, repo, snapshot, config_for)
    await publish_shards(factory, ctx, shards, store)
    ref = await service.build(ctx, repo, snapshot)
    report = await service.get(ctx, repo, snapshot, ref.id)
    doc = report.model_dump(mode="json")
    run = str(report.coverage.fact_runs[0])

    async def insert(inputs_digest=ref.inputs_digest, runs=(run,), document=doc, snap=snapshot):
        async with factory() as s, s.begin():
            await s.execute(
                text(
                    "INSERT INTO architecture_reports(org_id,id,repository_id,snapshot_id,principal_id,"
                    "schema_version,inputs_digest,report_digest,fact_runs,report_json,size_bytes) "
                    "VALUES(:o,:id,:r,:s,:p,1,:idig,:rdig,CAST(:runs AS jsonb),CAST(:doc AS jsonb),10)"
                ),
                {
                    "o": ctx.org_id,
                    "id": uuid4(),
                    "r": repo,
                    "s": snap,
                    "p": ctx.principal_id,
                    "idig": inputs_digest,
                    "rdig": "c" * 64,
                    "runs": json.dumps(list(runs)),
                    "doc": json.dumps(document),
                },
            )

    with pytest.raises(DBAPIError, match="input differs"):  # a run that does not exist
        await insert(
            runs=(str(uuid4()),), document={**doc, "coverage": {**doc["coverage"], "fact_runs": []}}
        )
    with pytest.raises(DBAPIError, match="identity differs"):  # digest not derived from the facts
        await insert(inputs_digest="d" * 64, document={**doc, "inputs_digest": "d" * 64})
    with pytest.raises(DBAPIError, match="identity differs"):  # document disagrees with its columns
        await insert(document={**doc, "snapshot_id": str(uuid4())})
    with pytest.raises(DBAPIError):  # the same inputs twice
        await insert()


async def test_capacity_stops_prepare_without_error_and_other_tenants_see_nothing(
    factory, tmp_path, monkeypatch
):
    from scripts.phase1_fixture import scenario

    ctx, snapshot, repo, store, service = await setup(factory, tmp_path)

    async def full(*args, **kwargs):
        raise CapacityExceeded("pending job quota reached")

    monkeypatch.setattr(service.structural, "create_analysis_spec", full)
    state = await service.prepare(ctx, repo, snapshot, config_for)
    assert state.shards[0].state == "not_started" and state.shards[0].job_id is None
    monkeypatch.undo()
    shards = await service.prepare(ctx, repo, snapshot, config_for)
    await publish_shards(factory, ctx, shards, store)
    ref = await service.build(ctx, repo, snapshot)
    _, other, *_ = await scenario(factory)
    with pytest.raises(ScopeViolation):
        await service.get(other, repo, snapshot, ref.id)
    assert shard_key.__name__ == "shard_key"
