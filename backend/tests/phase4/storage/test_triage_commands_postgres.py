"""Owned PostgreSQL qualification for atomic replay using the real API role."""

import asyncio

import pytest
from app.contracts.triage import TriageTransitionV1
from app.models.phase2_tables import PHASE2_TABLES
from app.models.scan_tables import finding_triage_events
from app.services.scan_publication import FindingFinalizer
from app.services.triage import TriageService
from scripts import phase2_db as harness
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from tests.phase4.storage.test_scan_publication import publication_case


async def test_concurrent_duplicate_commands_commit_one_response_and_event(
    api_factory, broker_factory, migration_factory, tmp_path, db_values
):
    ctx, spec, _, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    await FindingFinalizer(broker_factory).finish(lease, draft)
    # Apply the additive operation constraint to this marker-owned compatibility database.
    name = db_values["CODESPY_TEST_DATABASE"]
    harness.migrate(db_values, name, "0022_triage_commands")
    harness.grant_app(db_values, name)
    with harness.connect(db_values, name) as connection:
        revision_before = connection.execute(
            "SELECT version_num FROM public.alembic_version"
        ).fetchone()[0]
    service = TriageService(api_factory)
    gate = asyncio.Event()

    async def submit():
        await gate.wait()
        return await service.transition(
            ctx,
            spec.repository_id,
            spec.snapshot_id,
            draft.findings[0].id,
            TriageTransitionV1(state="confirmed", expected_version=0),
            request_key="concurrent-same-command",
        )

    tasks = [asyncio.create_task(submit()) for _ in range(2)]
    gate.set()
    first, second = await asyncio.gather(*tasks)
    assert first == second and first.version == 1
    async with api_factory() as session:
        assert await session.scalar(select(func.count()).select_from(finding_triage_events)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(PHASE2_TABLES["api_commands"]))
            == 1
        )

    with pytest.raises(IntegrityError):
        harness.migrate(
            db_values,
            db_values["CODESPY_TEST_DATABASE"],
            "0021_investigations_and_checkpoints",
            downgrade=True,
        )
    async with api_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(PHASE2_TABLES["api_commands"]))
            == 1
        )
    with harness.connect(db_values, db_values["CODESPY_TEST_DATABASE"]) as connection:
        assert (
            connection.execute("SELECT version_num FROM public.alembic_version").fetchone()[0]
            == revision_before
        )
