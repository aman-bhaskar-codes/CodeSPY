import asyncio

import pytest
from app.contracts.memory import (
    MemoryCreateRequestV1,
    MemoryKind,
    MemoryQueryRequestV1,
    MemoryReviewRequestV1,
)
from app.models.memory_tables import repo_memories
from app.models.tables import memberships
from app.services.memory_service import MemoryService
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from tests.retrieval.storage.test_index_guards import source_case


async def test_concurrent_proposals_review_and_database_expiry(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, spec, _ = await source_case(migration_factory, tmp_path)
    request = MemoryCreateRequestV1(
        org_id=ctx.org_id,
        repository_id=spec.repository_id,
        snapshot_id=spec.snapshot_id,
        principal_id=ctx.principal_id,
        memory_kind=MemoryKind.ARCHITECTURE_FACT,
        topic="adapters",
        summary="Scoped adapters",
        request_key="stable",
    )

    async def propose():
        async with api_factory() as s, s.begin():
            return await MemoryService(s).record_memory(request)

    a, b = await asyncio.gather(propose(), propose())
    assert a == b
    async with migration_factory() as s, s.begin():
        await s.execute(
            memberships.update().where(memberships.c.org_id == ctx.org_id).values(role="admin")
        )
    review = MemoryReviewRequestV1(
        action="accept", expected_version=1, request_key="accept", reason="Inspected"
    )
    async with api_factory() as s, s.begin():
        accepted = await MemoryService(s).review_memory(
            ctx.org_id, spec.repository_id, ctx.principal_id, a.memory_id, review
        )
        assert accepted.state == "accepted"
        query = MemoryQueryRequestV1(
            org_id=ctx.org_id,
            repository_id=spec.repository_id,
            principal_id=ctx.principal_id,
            snapshot_id=spec.snapshot_id,
            query="adapters",
        )
        assert len((await MemoryService(s).query_memory(query)).items) == 1
    # Fixture-owned DB only: expire relative to real database time; no production clock override.
    async with migration_factory() as s, s.begin():
        await s.execute(text("ALTER TABLE repo_memories DISABLE TRIGGER guard_memory_projection"))
        await s.execute(
            text(
                "UPDATE repo_memories SET expires_at=clock_timestamp()-interval '1 second', "
                "purge_after=clock_timestamp()-interval '1 second'"
            )
        )
        await s.execute(text("ALTER TABLE repo_memories ENABLE TRIGGER guard_memory_projection"))
    async with api_factory() as s, s.begin():
        assert not (await MemoryService(s).query_memory(query)).items
        with pytest.raises(Exception, match="context_invalidated"):
            await MemoryService(s).assert_current(
                ctx.org_id,
                spec.repository_id,
                ctx.principal_id,
                ((a.memory_id, accepted.version, accepted.content_digest),),
            )
    from app.models.memory_tables import memory_commands, memory_versions
    from app.workers.memory_maintenance import MemoryMaintenanceWorker

    assert await MemoryMaintenanceWorker(broker_factory).tick() == 1
    async with api_factory() as s:
        assert await s.scalar(select(memory_versions.c.payload).limit(1)) is None
        assert await s.scalar(select(memory_commands.c.request_digest).limit(1))
        assert await s.scalar(select(repo_memories.c.summary)) == ""


async def test_worker_cannot_accept_or_mutate_history(
    api_factory, broker_factory, migration_factory, tmp_path
):
    ctx, spec, _ = await source_case(migration_factory, tmp_path)
    async with api_factory() as s, s.begin():
        entry = await MemoryService(s).record_memory(
            MemoryCreateRequestV1(
                org_id=ctx.org_id,
                repository_id=spec.repository_id,
                snapshot_id=spec.snapshot_id,
                principal_id=ctx.principal_id,
                memory_kind=MemoryKind.ARCHITECTURE_FACT,
                topic="test",
                summary="candidate",
            )
        )
    async with broker_factory() as s:
        assert not await s.scalar(
            text("SELECT has_table_privilege(current_user,'repo_memories','UPDATE')")
        )
        assert await s.scalar(
            text("SELECT has_column_privilege(current_user,'repo_memories','updated_at','UPDATE')")
        )
        with pytest.raises(DBAPIError):
            await s.execute(
                repo_memories.update()
                .where(repo_memories.c.id == entry.memory_id)
                .values(state="accepted")
            )
        await s.rollback()
    async with api_factory() as s:
        with pytest.raises(DBAPIError):
            await s.execute(text("UPDATE memory_versions SET payload='{}'::jsonb"))
        await s.rollback()
        assert await s.scalar(select(repo_memories.c.state)) == "candidate"


@pytest.mark.parametrize(
    "summary", ["Scoped adapters", " scope" * 600], ids=["selected", "omitted"]
)
async def test_context_selection_is_frozen_and_revalidated(
    api_factory, broker_factory, migration_factory, tmp_path, summary
):
    from uuid import uuid4

    from app.adapters.model.openai import OpenAIResponsesCodec
    from app.contracts.context_manifest import MemoryContextSettingsV1
    from app.models.investigation_tables import investigations
    from app.services.investigation_context import InvestigationContextService
    from tests.model_connections.unit.test_codecs import request
    from tests.model_connections.unit.test_registry import profile

    ctx, spec, _ = await source_case(migration_factory, tmp_path)
    inv = uuid4()
    async with migration_factory() as s, s.begin():
        await s.execute(
            memberships.update().where(memberships.c.org_id == ctx.org_id).values(role="owner")
        )
        await s.execute(
            investigations.insert().values(
                org_id=ctx.org_id,
                id=inv,
                repository_id=spec.repository_id,
                snapshot_id=spec.snapshot_id,
                principal_id=ctx.principal_id,
                target_type="repository",
                query="adapters",
                budget_limit_micros=10000,
            )
        )
    async with api_factory() as s, s.begin():
        svc = MemoryService(s)
        entry = await svc.record_memory(
            MemoryCreateRequestV1(
                org_id=ctx.org_id,
                repository_id=spec.repository_id,
                snapshot_id=spec.snapshot_id,
                principal_id=ctx.principal_id,
                memory_kind=MemoryKind.ARCHITECTURE_FACT,
                topic="adapters",
                summary=summary,
            )
        )
        accepted = await svc.review_memory(
            ctx.org_id,
            spec.repository_id,
            ctx.principal_id,
            entry.memory_id,
            MemoryReviewRequestV1(
                action="accept", expected_version=1, request_key="accept", reason="Checked"
            ),
        )
    p = profile("openai_fast")
    base = request(p, investigation_id=inv)
    base = base.model_copy(
        update={
            "messages": (
                base.messages[0],
                base.messages[-1].model_copy(update={"content": "adapters"}),
            )
        }
    )
    settings = MemoryContextSettingsV1(mode="reviewed")
    async with broker_factory() as s, s.begin():
        service = InvestigationContextService(s, MemoryService(s), settings)
        first, manifest = await service.prepare(
            ctx.org_id,
            spec.repository_id,
            ctx.principal_id,
            base,
            p,
            OpenAIResponsesCodec(),
            "a" * 64,
        )
        assert len(manifest.memory_references) == (1 if len(summary) < 100 else 0)
    async with broker_factory() as s, s.begin():
        second, replay = await InvestigationContextService(s, MemoryService(s), settings).prepare(
            ctx.org_id,
            spec.repository_id,
            ctx.principal_id,
            base,
            p,
            OpenAIResponsesCodec(),
            "a" * 64,
        )
        assert replay == manifest and first.prepared == second.prepared
    async with api_factory() as s, s.begin():
        await MemoryService(s).review_memory(
            ctx.org_id,
            spec.repository_id,
            ctx.principal_id,
            entry.memory_id,
            MemoryReviewRequestV1(
                action="archive",
                expected_version=accepted.version,
                request_key="archive",
                reason="Withdrawn",
            ),
        )
    async with broker_factory() as s, s.begin():
        if manifest.memory_references:
            with pytest.raises(Exception, match="context_invalidated"):
                await InvestigationContextService(s, MemoryService(s), settings).assert_current(
                    manifest
                )
        else:
            await InvestigationContextService(s, MemoryService(s), settings).assert_current(
                manifest
            )


async def test_empty_context_still_requires_retained_target_source(
    broker_factory, migration_factory, tmp_path, db_values
):
    from uuid import uuid4

    from app.adapters.model.openai import OpenAIResponsesCodec
    from app.contracts.context_manifest import MemoryContextSettingsV1
    from app.models.investigation_tables import investigations
    from app.models.phase2_tables import PHASE2_TABLES
    from app.services.investigation_context import InvestigationContextService
    from tests.model_connections.unit.test_codecs import request
    from tests.model_connections.unit.test_registry import profile

    ctx, spec, _ = await source_case(migration_factory, tmp_path)
    inv = uuid4()
    async with migration_factory() as s, s.begin():
        await s.execute(
            investigations.insert().values(
                org_id=ctx.org_id,
                id=inv,
                repository_id=spec.repository_id,
                snapshot_id=spec.snapshot_id,
                principal_id=ctx.principal_id,
                target_type="repository",
                query="unmatched",
                budget_limit_micros=10000,
            )
        )
    settings = MemoryContextSettingsV1(mode="reviewed")
    p = profile("openai_fast")
    async with broker_factory() as s, s.begin():
        _, manifest = await InvestigationContextService(s, MemoryService(s), settings).prepare(
            ctx.org_id,
            spec.repository_id,
            ctx.principal_id,
            request(p, investigation_id=inv),
            p,
            OpenAIResponsesCodec(),
            "a" * 64,
        )
        assert not manifest.memory_references
    from scripts import phase2_db as harness

    with pytest.raises(
        Exception,
        match=(
            r"memory review history must be preserved"
            r"|scoped ciphertext authority must be retained"
            r"|report memory processing history must be retained"
            r"|embedding cancellation history must be retained"
            r"|runtime authority repair must be retained"
        ),
    ):
        harness.migrate(
            db_values,
            db_values["CODESPY_TEST_DATABASE"],
            "0031_investigation_execution",
            downgrade=True,
        )
    async with migration_factory() as s, s.begin():
        await s.execute(PHASE2_TABLES["object_records"].update().values(state="deleted"))
    async with broker_factory() as s, s.begin():
        with pytest.raises(Exception, match="context_invalidated"):
            await InvestigationContextService(s, MemoryService(s), settings).assert_current(
                manifest
            )
