"""Real API-role authority and serialized triage checks on an owned PostgreSQL database."""

import asyncio
from uuid import uuid4

import pytest
from app.contracts.triage import TriageTransitionV1
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.models.phase2_tables import PHASE2_TABLES
from app.models.scan_tables import finding_triage_events
from app.models.tables import memberships, principals
from app.services.findings_query import FindingsQueryService
from app.services.scan_publication import FindingFinalizer
from app.services.triage import TriageService
from sqlalchemy import func, select, update

from tests.phase4.storage.test_scan_publication import publication_case


async def test_public_finding_reads_and_triage_recheck_current_grant(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, spec, _, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    await FindingFinalizer(broker_factory).finish(lease, draft)
    finding_id = draft.findings[0].id
    reads, triage = FindingsQueryService(api_factory), TriageService(api_factory)
    detail = await reads.get_finding_detail(ctx, spec.repository_id, spec.snapshot_id, finding_id)
    assert detail.id == finding_id
    record = await triage.transition(
        ctx,
        spec.repository_id,
        spec.snapshot_id,
        finding_id,
        TriageTransitionV1(state="confirmed", expected_version=0),
    )
    assert record.version == 1
    async with migration_factory() as session, session.begin():
        grants = PHASE2_TABLES["repository_grants"]
        await session.execute(
            update(grants)
            .where(
                grants.c.org_id == ctx.org_id,
                grants.c.repository_id == spec.repository_id,
                grants.c.principal_id == ctx.principal_id,
            )
            .values(revoked_at=func.clock_timestamp())
        )
    with pytest.raises(ScopeViolation):
        await reads.get_finding_detail(ctx, spec.repository_id, spec.snapshot_id, finding_id)
    with pytest.raises(ScopeViolation):
        await triage.transition(
            ctx,
            spec.repository_id,
            spec.snapshot_id,
            finding_id,
            TriageTransitionV1(state="unreviewed", expected_version=1),
        )


async def test_two_principals_cannot_both_create_initial_triage_projection(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, spec, _, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    await FindingFinalizer(broker_factory).finish(lease, draft)
    other_id = uuid4()
    async with migration_factory() as session, session.begin():
        await session.execute(
            principals.insert().values(
                id=other_id, subject_key=str(other_id), display_name="Other reviewer"
            )
        )
        await session.execute(
            memberships.insert().values(org_id=ctx.org_id, principal_id=other_id, role="member")
        )
        await session.execute(
            PHASE2_TABLES["repository_grants"]
            .insert()
            .values(
                org_id=ctx.org_id,
                principal_id=other_id,
                repository_id=spec.repository_id,
                permission="write",
            )
        )
    other = ctx.model_copy(update={"principal_id": other_id})
    start = asyncio.Event()

    async def submit(principal):
        await start.wait()
        return await TriageService(api_factory).transition(
            principal,
            spec.repository_id,
            spec.snapshot_id,
            draft.findings[0].id,
            TriageTransitionV1(state="confirmed", expected_version=0),
        )

    pending = [asyncio.create_task(submit(principal)) for principal in (ctx, other)]
    start.set()
    results = await asyncio.gather(*pending, return_exceptions=True)
    assert sum(isinstance(result, DuplicateConflict) for result in results) == 1
    winners = [result for result in results if not isinstance(result, BaseException)]
    assert len(winners) == 1 and winners[0].version == 1
    async with migration_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(finding_triage_events)
            .where(
                finding_triage_events.c.org_id == ctx.org_id,
                finding_triage_events.c.snapshot_id == spec.snapshot_id,
            )
        )
        assert count == 1
