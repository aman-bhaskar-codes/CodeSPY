"""Published report -> local outbox -> candidate, with a real isolated source reader."""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from app.contracts.investigation_execution import (
    ClaimVerificationStatus,
    ClaimVerificationV1,
    InvestigationClaimKind,
    InvestigationClaimV2,
    InvestigationEvidenceV1,
    InvestigationReportDraftV1,
    InvestigationStopReason,
)
from app.contracts.investigation_jobs import InvestigationCreateRequestV2
from app.contracts.memory import MemoryQueryRequestV1
from app.models.investigation_execution_tables import investigation_evidence, investigation_reports
from app.models.memory_tables import repo_memories
from app.models.phase2_tables import PHASE2_TABLES
from app.models.report_memory_tables import report_memory_receipts
from app.models.tables import source_files
from app.scanners.docker import DockerController
from app.scanners.ownership import ScannerOwnership
from app.services.investigation_jobs import InvestigationJobService
from app.services.jobs import JobService
from app.services.memory_service import MemoryService
from app.source_read.adapter import DockerSourceReader
from app.workers.dispatcher import OutboxDispatcher
from app.workers.investigation_finalizer import InvestigationFinalizer
from app.workers.report_memory import ReportMemoryProposalWorker
from sqlalchemy import func, insert, select, update
from tests.phase3.integration.test_structural_publication import admitted


@pytest.mark.parametrize("revoked", [False, True])
async def test_report_candidates_are_reviewed_deduplicated_and_separate_from_queue(
    migration_factory, broker_factory, tmp_path, revoked
):
    image = os.environ.get("CODESPY_SOURCE_READER_IMAGE")
    if image is None:
        pytest.skip("owned isolated source-reader image required")
    ctx, snapshot, repo, store = await admitted(migration_factory, tmp_path)
    reader = DockerSourceReader(
        ScannerOwnership.create(tmp_path / "report-reader", image),
        DockerController(
            Path("/usr/local/bin/docker"),
            Path.home() / ".docker/run/docker.sock",
            tmp_path / "docker-config",
        ),
        store,
    )
    commands = InvestigationJobService(migration_factory)
    created = await commands.create(
        ctx,
        InvestigationCreateRequestV2(
            repository_id=repo,
            snapshot_id=snapshot,
            query="Inspect source observation",
            target_type="repository",
        ),
        request_key="report-create",
    )
    started = await commands.start(ctx, created.id, request_key="report-start")
    lease = await JobService(broker_factory).claim(ctx.org_id, started.job_id, 1, "report-worker")
    assert lease is not None
    async with migration_factory() as session, session.begin():
        file = (
            (
                await session.execute(
                    select(source_files).where(
                        source_files.c.org_id == ctx.org_id,
                        source_files.c.snapshot_id == snapshot,
                        source_files.c.path == "a.py",
                    )
                )
            )
            .mappings()
            .one()
        )
        evidence = InvestigationEvidenceV1(
            id=uuid4(),
            org_id=ctx.org_id,
            investigation_id=created.id,
            snapshot_id=snapshot,
            source_path=file["path"],
            file_sha256=file["sha256"],
            original_start_line=1,
            original_end_line=1,
            safe_start_line=1,
            safe_end_line=1,
            excerpt_sha256=file["sha256"],
            observation_type="source",
        )
        await session.execute(
            insert(investigation_evidence).values(**evidence.model_dump(exclude_none=True))
        )
    claim = InvestigationClaimV2(
        claim_id=uuid4(),
        statement="The source contains a print call.",
        kind=InvestigationClaimKind.SOURCE_OBSERVATION,
        evidence_ids=(evidence.id,),
    )
    draft = InvestigationReportDraftV1(
        investigation_id=created.id,
        spec_hash=created.spec_hash,
        verdict="indeterminate",
        summary="Synthetic verified source observation",
        claims=(claim,),
        verifications=(
            ClaimVerificationV1(
                claim_id=claim.claim_id,
                status=ClaimVerificationStatus.SOURCE_CORRESPONDENCE_CONFIRMED,
            ),
        ),
        evidence_refs=(evidence.id,),
        stop_reason=InvestigationStopReason.COMPLETED,
        draft_digest="a" * 64,
    )
    draft = draft.model_copy(update={"draft_digest": draft.compute_draft_digest()})
    finalizer = InvestigationFinalizer(broker_factory, memory_proposals=True)
    publication = await finalizer.publish(ctx, lease, draft)
    assert await finalizer.publish(ctx, lease, draft) == publication
    outbox = PHASE2_TABLES["outbox"]
    async with migration_factory() as session, session.begin():
        await session.execute(
            update(outbox).where(outbox.c.kind != "memory_proposals").values(state="sent")
        )
        if revoked:
            await session.execute(
                PHASE2_TABLES["repository_grants"]
                .update()
                .where(PHASE2_TABLES["repository_grants"].c.org_id == ctx.org_id)
                .values(permission="read")
            )

    class Queue:
        async def send(self, body):
            raise AssertionError("memory intents must never reach the job queue")

    assert await OutboxDispatcher(broker_factory, Queue()).dispatch_once() == 0
    worker = ReportMemoryProposalWorker(broker_factory, reader)
    try:
        results = await asyncio.gather(worker.tick(), worker.tick())
        assert sum(results) == 1
        async with broker_factory() as session:
            receipt = (await session.execute(select(report_memory_receipts))).mappings().one()
            entries = (await session.execute(select(repo_memories))).mappings().all()
            assert receipt["outcome"] == ("blocked" if revoked else "created")
            assert len(entries) == (0 if revoked else 1)
            assert all(e["state"] == "candidate" for e in entries)
            assert (
                await session.scalar(select(func.count()).select_from(investigation_reports)) == 1
            )
            if not revoked:
                found = await MemoryService(session).query_memory(
                    MemoryQueryRequestV1(
                        org_id=ctx.org_id,
                        repository_id=repo,
                        principal_id=ctx.principal_id,
                        snapshot_id=snapshot,
                        query="print",
                    )
                )
                assert found.items == ()
                page = await MemoryService(session).list_memories(
                    ctx.org_id, repo, ctx.principal_id, snapshot_id=snapshot
                )
                assert len(page.items) == 1 and page.items[0].state == "candidate"

        if not revoked:
            from app.contracts.memory import MemoryReviewRequestV1
            from app.models.tables import memberships
            from app.services.memory_evidence import SourceMemoryEvidence

            async with migration_factory() as session, session.begin():
                await session.execute(
                    memberships.update()
                    .where(memberships.c.org_id == ctx.org_id)
                    .values(role="owner")
                )
            async with broker_factory() as session, session.begin():
                # A worker connection must still be unable to approve its proposal.
                with pytest.raises(
                    Exception, match=r"permission denied|worker cannot review|worker memory"
                ):
                    await MemoryService(
                        session, evidence=SourceMemoryEvidence(session, broker_factory, reader)
                    ).review_memory(
                        ctx.org_id,
                        repo,
                        ctx.principal_id,
                        page.items[0].memory_id,
                        MemoryReviewRequestV1(
                            action="accept",
                            expected_version=1,
                            request_key="worker-forbidden",
                            reason="Fixture denial",
                        ),
                    )
            async with migration_factory() as session, session.begin():
                service = MemoryService(
                    session, evidence=SourceMemoryEvidence(session, migration_factory, reader)
                )
                accepted = await service.review_memory(
                    ctx.org_id,
                    repo,
                    ctx.principal_id,
                    page.items[0].memory_id,
                    MemoryReviewRequestV1(
                        action="accept",
                        expected_version=1,
                        request_key="human-review",
                        reason="Fixture source reviewed",
                    ),
                )
                assert accepted.state == "accepted"
            async with broker_factory() as session:
                result = await MemoryService(session).query_memory(
                    MemoryQueryRequestV1(
                        org_id=ctx.org_id,
                        repository_id=repo,
                        principal_id=ctx.principal_id,
                        snapshot_id=snapshot,
                        query="print",
                    )
                )
                assert len(result.items) == 1 and result.items[0].state == "accepted"
        assert await worker.tick() == 0
        # Simulated acknowledgment loss: same durable origin cannot produce another candidate.
        async with migration_factory() as session, session.begin():
            await session.execute(
                update(outbox)
                .where(outbox.c.kind == "memory_proposals")
                .values(state="pending", claim_token=None, claim_expires_at=None)
            )
        await worker.tick()
        async with broker_factory() as session:
            assert await session.scalar(select(func.count()).select_from(repo_memories)) == (
                0 if revoked else 1
            )
    finally:
        await reader.recover_owned()
