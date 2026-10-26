"""Real database authority for attempt-bound dispatch and retained sources."""

from uuid import uuid4

import pytest
from app.contracts.investigation_jobs import InvestigationCreateRequestV2
from app.domain.errors import InvalidTransition, ScopeViolation
from app.models.phase2_tables import PHASE2_TABLES
from app.services.investigation_checkpointer import StaleWorkerFencingError
from app.services.investigation_jobs import InvestigationJobService
from app.services.investigation_runtime import BoundModelDispatchAuthority
from app.services.investigation_support import SqlInvestigationSupport
from app.services.jobs import JobService
from sqlalchemy import text
from tests.retrieval.storage.test_index_guards import source_case


@pytest.mark.parametrize("denial", ["missing_source", "expired_lease", "revoked"])
async def test_bound_dispatch_revalidates_real_authority(
    migration_factory, broker_factory, tmp_path, denial
):
    ctx, spec, _ = await source_case(migration_factory, tmp_path)
    commands = InvestigationJobService(migration_factory)
    investigation = await commands.create(
        ctx,
        InvestigationCreateRequestV2(
            repository_id=spec.repository_id,
            snapshot_id=spec.snapshot_id,
            query="Check fixture",
            target_type="repository",
        ),
        request_key="create-bound",
    )
    started = await commands.start(ctx, investigation.id, request_key="start-bound")
    lease = await JobService(migration_factory).claim(ctx.org_id, started.job_id, 1, "bound-worker")
    assert lease is not None
    gate = BoundModelDispatchAuthority(lease, SqlInvestigationSupport(broker_factory))
    async with broker_factory() as session, session.begin():
        await gate.assert_dispatch_allowed(session, investigation.id, uuid4())
    async with migration_factory() as session, session.begin():
        if denial == "missing_source":
            await session.execute(PHASE2_TABLES["object_records"].update().values(state="deleted"))
        elif denial == "expired_lease":
            await session.execute(
                text(
                    "UPDATE job_attempts "
                    "SET lease_expires_at=clock_timestamp()-interval '1 second' "
                    "WHERE org_id=:org AND id=:attempt"
                ),
                {"org": ctx.org_id, "attempt": lease.attempt_id},
            )
        else:
            await session.execute(
                PHASE2_TABLES["repository_grants"].update().values(permission="read")
            )
    async with broker_factory() as session, session.begin():
        with pytest.raises((InvalidTransition, ScopeViolation, StaleWorkerFencingError)):
            await gate.assert_dispatch_allowed(session, investigation.id, uuid4())
