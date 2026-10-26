"""Direct SQL cannot commit incomplete results or mutate sealed scan history."""

import pytest
from app.contracts.scan_publication import project_scan
from app.domain.canonical_json import canonical_bytes, load_json
from app.models.scan_tables import scan_reports
from app.models.tables import analysis_runs
from app.repositories.job_repository import worker_context
from app.services.scan_publication import FindingFinalizer
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError

from tests.phase4.storage.test_scan_publication import publication_case
from tests.phase4.storage.test_scan_races import assert_no_publication


@pytest.mark.parametrize("context", [False, True])
async def test_report_only_cannot_commit_even_with_current_worker_context(
    api_factory, broker_factory, migration_factory, tmp_path, context
):
    _, spec, _, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    projection = project_scan(draft)
    with pytest.raises(DBAPIError):
        async with broker_factory() as session, session.begin():
            if context:
                await worker_context(session, lease)
            raw = canonical_bytes(draft.model_dump()).decode()
            await session.execute(
                insert(scan_reports).values(
                    org_id=lease.org_id,
                    snapshot_id=lease.run_ref.snapshot_id,
                    run_id=lease.run_ref.run_id,
                    repository_id=spec.repository_id,
                    spec_id=spec.id,
                    config_hash=spec.config_hash,
                    draft_json=load_json(raw),
                    canonical_text=raw,
                    draft_digest=projection.draft_digest,
                    batch_hash=projection.batch_hash,
                    coverage=draft.coverage,
                    omitted_findings=draft.omitted_findings,
                )
            )
    await assert_no_publication(broker_factory)


async def test_generic_persistence_cannot_seal_scan_without_complete_report(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, _, _, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    finalizer = FindingFinalizer(broker_factory)
    with pytest.raises(DBAPIError):
        async with broker_factory() as session, session.begin():
            await worker_context(session, lease)
            version = await session.scalar(
                select(analysis_runs.c.write_version).where(
                    analysis_runs.c.org_id == lease.org_id,
                    analysis_runs.c.id == lease.run_ref.run_id,
                )
            )
            await finalizer.jobs.persistence._persist_result_in_session(
                session, ctx, lease.run_ref, project_scan(draft).batch, version
            )
    await assert_no_publication(broker_factory)


async def test_published_report_is_immutable_even_for_migration_role(
    api_factory, broker_factory, migration_factory, tmp_path
):
    _, _, _, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    await FindingFinalizer(broker_factory).finish(lease, draft)
    with pytest.raises(DBAPIError):
        async with migration_factory() as session, session.begin():
            await session.execute(update(scan_reports).values(coverage="unknown"))
    with pytest.raises(DBAPIError):
        async with broker_factory() as session, session.begin():
            await session.execute(text("TRUNCATE scan_reports CASCADE"))


async def test_sql_rejects_duplicate_report_evidence_hiding_an_extra_stored_record(
    api_factory, broker_factory, migration_factory, tmp_path, monkeypatch
):
    from app.domain.canonical_json import semantic_digest
    from app.services import scan_publication

    _, _, _, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path, two=True
    )
    assert len(draft.evidence) == 2
    payload = draft.model_dump()
    payload["evidence"] = [payload["evidence"][0], payload["evidence"][0]]
    projection = project_scan(draft).model_copy(update={"draft_digest": semantic_digest(payload)})
    original = scan_publication.canonical_bytes

    def forged(value):
        return original(payload if isinstance(value, dict) and "native_results" in value else value)

    monkeypatch.setattr(scan_publication, "canonical_bytes", forged)
    monkeypatch.setattr(scan_publication, "project_scan", lambda _: projection)
    with pytest.raises(DBAPIError):
        await FindingFinalizer(broker_factory).finish(lease, draft)
    await assert_no_publication(broker_factory)


async def test_sql_rejects_omitted_configured_operations_even_when_all_projection_sets_are_empty(
    api_factory, broker_factory, migration_factory, tmp_path, monkeypatch
):
    from app.domain.canonical_json import semantic_digest
    from app.repositories.evidence_repository import EvidenceRepository
    from app.services import scan_publication

    _, _, _, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    payload = {
        **draft.model_dump(),
        "native_results": [],
        "dependency_result": None,
        "findings": [],
        "evidence": [],
        "decisions": [],
    }
    original = scan_publication.canonical_bytes

    def forged(value):
        return original(payload if isinstance(value, dict) and "native_results" in value else value)

    projected = project_scan(draft)
    batch = projected.batch.model_copy(update={"evidence": []})
    projected = projected.model_copy(
        update={
            "observations": (),
            "findings": (),
            "decisions": (),
            "support": (),
            "batch": batch,
            "batch_hash": batch.semantic_hash(),
            "draft_digest": semantic_digest(payload),
        }
    )

    async def skip_evidence(*args):
        return None

    monkeypatch.setattr(scan_publication, "canonical_bytes", forged)
    monkeypatch.setattr(scan_publication, "project_scan", lambda _: projected)
    monkeypatch.setattr(EvidenceRepository, "insert_batch", skip_evidence)
    with pytest.raises(DBAPIError):
        await FindingFinalizer(broker_factory).finish(lease, draft)
    await assert_no_publication(broker_factory)


async def test_sql_rejects_attempt_runtime_differing_from_immutable_config(
    api_factory, broker_factory, migration_factory, tmp_path, monkeypatch
):
    from app.contracts.scan_jobs import ScanConfigV1
    from app.services.jobs import JobService
    from app.services.scan_jobs import ScanJobService

    from tests.phase4.storage.test_scan_jobs import Registry, scenario

    ctx, snapshot, repo, _, config = await scenario(migration_factory, tmp_path)
    _, job = await ScanJobService(api_factory, Registry()).create(
        ctx, repo, snapshot, config, "binding"
    )
    original = ScanConfigV1.publication_spec

    def changed(self, *args):
        return original(self, *args).model_copy(update={"runtime_digest": "f" * 64})

    monkeypatch.setattr(ScanConfigV1, "publication_spec", changed)
    from app.domain.errors import InvalidTransition

    with pytest.raises(InvalidTransition) as error:
        await JobService(broker_factory).claim(ctx.org_id, job.id, 1, "altered")
    assert error.value.sqlstate == "23514"
    assert (await JobService(broker_factory).get(ctx, job.id)).state == "queued"


@pytest.mark.parametrize(
    "mutation, message",
    [
        ("request", "native scan result binding differs"),
        ("selection", "native scan result binding differs"),
        ("coverage", "scan aggregate coverage differs"),
    ],
)
async def test_sql_rejects_native_binding_and_coverage_tampering(
    api_factory, broker_factory, migration_factory, tmp_path, monkeypatch, mutation, message
):
    from app.domain.canonical_json import semantic_digest
    from app.services import scan_publication

    _, _, _, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    payload = draft.model_dump()
    result = payload["native_results"][0]["result"]
    if mutation == "request":
        result["request_digest"] = "f" * 64
    elif mutation == "selection":
        result["processed_file_ids"] = []
    else:
        result["status"] = "partial"
    projected = project_scan(draft).model_copy(update={"draft_digest": semantic_digest(payload)})
    original = scan_publication.canonical_bytes

    def forged(value):
        return original(payload if isinstance(value, dict) and "native_results" in value else value)

    monkeypatch.setattr(scan_publication, "canonical_bytes", forged)
    monkeypatch.setattr(scan_publication, "project_scan", lambda _: projected)
    with pytest.raises(DBAPIError, match=message):
        await FindingFinalizer(broker_factory).finish(lease, draft)
    await assert_no_publication(broker_factory)
