"""Real accounting and memory authority around a synthetic HTTP provider transport."""

from uuid import uuid4

import pytest
from app.contracts.context_manifest import MemoryContextSettingsV1
from app.contracts.memory import (
    EvidencePointerV1,
    MemoryCreateRequestV1,
    MemoryKind,
    MemoryReviewRequestV1,
)
from app.domain.errors import InvalidTransition
from app.models.investigation_tables import investigations
from app.models.model_gateway_tables import model_reservations
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import artifacts, memberships, source_snapshots
from app.services.memory_service import MemoryService
from sqlalchemy import select
from tests.model_connections.storage.test_configured_gateway import configured


async def test_bound_evidence_reaches_provider_and_withdrawal_prevents_next_dispatch(
    migration_factory, tmp_path
):
    gateway, args, client, calls = await configured(migration_factory, tmp_path)
    org, repo, actor = args["org_id"], args["repository_id"], args["principal_id"]

    class Evidence:
        async def verify(self, org, repo, snapshot, actor, pointer):
            assert pointer.file_path == "a.py" and pointer.end_line == 1

        async def verify_current(self, org, repo, snapshot, actor, pointer):
            assert pointer.file_path == "a.py" and pointer.end_line == 1

    evidence = Evidence()
    async with migration_factory() as s, s.begin():
        snapshot = await s.scalar(
            select(source_snapshots.c.id)
            .join(
                artifacts,
                (artifacts.c.org_id == source_snapshots.c.org_id)
                & (artifacts.c.id == source_snapshots.c.artifact_id),
            )
            .join(
                PHASE2_TABLES["object_records"],
                (PHASE2_TABLES["object_records"].c.org_id == artifacts.c.org_id)
                & (PHASE2_TABLES["object_records"].c.key == artifacts.c.storage_key)
                & (PHASE2_TABLES["object_records"].c.state == "available"),
            )
            .where(
                source_snapshots.c.org_id == org,
                source_snapshots.c.repository_id == repo,
                source_snapshots.c.state == "sealed",
            )
        )
        await s.execute(
            memberships.update().where(memberships.c.org_id == org).values(role="owner")
        )
        await s.execute(
            investigations.insert().values(
                org_id=org,
                id=args["investigation_id"],
                repository_id=repo,
                snapshot_id=snapshot,
                principal_id=actor,
                target_type="repository",
                query="synthetic test",
                budget_limit_micros=100000,
            )
        )
        service = MemoryService(s, evidence=evidence)
        proposal = await service.record_memory(
            MemoryCreateRequestV1(
                org_id=org,
                repository_id=repo,
                snapshot_id=snapshot,
                principal_id=actor,
                memory_kind=MemoryKind.ARCHITECTURE_FACT,
                topic="synthetic test",
                summary="bounded_memory_marker",
                evidence_pointers=(EvidencePointerV1(file_path="a.py", start_line=1, end_line=1),),
            )
        )
        accepted = await service.review_memory(
            org,
            repo,
            actor,
            proposal.memory_id,
            MemoryReviewRequestV1(
                action="accept", expected_version=1, request_key="accept", reason="Fixture checked"
            ),
        )
    gateway.memory_context = MemoryContextSettingsV1(mode="reviewed")
    gateway.memory_evidence_factory = lambda session: evidence
    try:
        result = await gateway.execute_call(**args)
        assert result.content == "answer"
        assert len(calls) == 1 and b"bounded_memory_marker" in calls[0].content
        delegate = gateway.transports["openai"]

        class WithdrawBeforeDispatch:
            def prepare(self, request, profile):
                return delegate.prepare(request, profile)

            async def invoke(self, prepared, *, before_dispatch=None):
                async with migration_factory() as s, s.begin():
                    await MemoryService(s).review_memory(
                        org,
                        repo,
                        actor,
                        proposal.memory_id,
                        MemoryReviewRequestV1(
                            action="archive",
                            expected_version=accepted.version,
                            request_key="withdraw",
                            reason="Withdrawn before dispatch",
                        ),
                    )
                return await delegate.invoke(prepared, before_dispatch=before_dispatch)

            async def aclose(self):
                await delegate.aclose()

        gateway.transports["openai"] = WithdrawBeforeDispatch()
        next_args = args | {"logical_call_id": uuid4(), "idempotency_key": "withdrawn-call"}
        with pytest.raises(InvalidTransition, match="context_invalidated"):
            await gateway.execute_call(**next_args)
        assert len(calls) == 1
        async with migration_factory() as s:
            states = (
                (
                    await s.execute(
                        select(model_reservations.c.state).order_by(model_reservations.c.created_at)
                    )
                )
                .scalars()
                .all()
            )
            assert states == ["settled", "rejected_before_dispatch"]
    finally:
        await client.aclose()
