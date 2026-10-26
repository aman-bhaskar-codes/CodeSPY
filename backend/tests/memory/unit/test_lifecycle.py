from uuid import uuid4

import pytest
import sqlalchemy as sa
from app.contracts.memory import (
    MemoryCreateRequestV1,
    MemoryKind,
    MemoryQueryRequestV1,
    MemoryReviewRequestV1,
)
from app.domain.errors import DuplicateConflict, InvalidTransition, PermissionDenied
from app.models.memory_tables import repo_memories
from app.models.tables import memberships
from app.services.memory_service import MemoryService
from tests.phase5.unit.test_memory_service import seed_authorized_principal, seed_snapshot


async def setup_memory(session):
    org, repo, actor, snap = (uuid4() for _ in range(4))
    await seed_authorized_principal(session, org, repo, actor)
    await seed_snapshot(session, org, repo, snap)
    svc = MemoryService(session)
    req = MemoryCreateRequestV1(
        org_id=org,
        repository_id=repo,
        principal_id=actor,
        snapshot_id=snap,
        memory_kind=MemoryKind.ARCHITECTURE_FACT,
        topic="routing",
        summary="Requests use scoped adapters",
        request_key="proposal",
    )
    return svc, req


async def test_candidate_is_not_retrievable_and_review_requires_admin(session):
    svc, req = await setup_memory(session)
    entry = await svc.record_memory(req)
    assert entry.state == "candidate"
    query = MemoryQueryRequestV1(
        org_id=req.org_id,
        repository_id=req.repository_id,
        principal_id=req.principal_id,
        snapshot_id=req.snapshot_id,
        query="routing",
    )
    assert not (await svc.query_memory(query)).items
    review = MemoryReviewRequestV1(
        action="accept", expected_version=1, request_key="review", reason="Checked"
    )
    with pytest.raises(PermissionDenied):
        await svc.review_memory(
            req.org_id, req.repository_id, req.principal_id, entry.memory_id, review
        )
    await session.execute(memberships.update().values(role="admin"))
    accepted = await svc.review_memory(
        req.org_id, req.repository_id, req.principal_id, entry.memory_id, review
    )
    assert accepted.state == "accepted"
    assert accepted.expires_at > accepted.accepted_at
    assert (await svc.query_memory(query)).items[0].version == accepted.version
    assert (
        await svc.review_memory(
            req.org_id, req.repository_id, req.principal_id, entry.memory_id, review
        )
        == accepted
    )
    with pytest.raises(DuplicateConflict):
        await svc.review_memory(
            req.org_id,
            req.repository_id,
            req.principal_id,
            entry.memory_id,
            review.model_copy(update={"reason": "different"}),
        )
    with pytest.raises(InvalidTransition):
        await svc.review_memory(
            req.org_id,
            req.repository_id,
            req.principal_id,
            entry.memory_id,
            review.model_copy(update={"request_key": "stale"}),
        )


async def test_proposal_replay_is_bound_to_actor_and_payload(session):
    svc, req = await setup_memory(session)
    entry = await svc.record_memory(req)
    assert (await svc.record_memory(req)).memory_id == entry.memory_id
    with pytest.raises(DuplicateConflict):
        await svc.record_memory(req.model_copy(update={"summary": "conflict"}))


async def test_archive_excludes_memory_and_restore_versions_it(session):
    svc, req = await setup_memory(session)
    entry = await svc.record_memory(req)
    await session.execute(memberships.update().values(role="owner"))

    async def review(action, version):
        return await svc.review_memory(
            req.org_id,
            req.repository_id,
            req.principal_id,
            entry.memory_id,
            MemoryReviewRequestV1(
                action=action, expected_version=version, request_key=str(uuid4()), reason="Reviewed"
            ),
        )

    accepted = await review("accept", 1)
    archived = await review("archive", accepted.version)
    assert archived.state == "archived"
    restored = await review("restore", archived.version)
    assert restored.version > archived.version
    assert restored.state == "accepted"


async def test_purge_removes_payload_and_preserves_audit(session):
    from datetime import UTC, datetime, timedelta

    from app.models.memory_tables import memory_commands, memory_versions

    svc, req = await setup_memory(session)
    await svc.record_memory(req)
    await session.execute(
        repo_memories.update().values(purge_after=datetime.now(UTC) - timedelta(seconds=1))
    )
    assert await svc.purge_expired() == 1
    assert await session.scalar(sa.select(memory_versions.c.payload)) is None
    assert await session.scalar(sa.select(memory_commands.c.request_digest))
    assert await session.scalar(sa.select(repo_memories.c.summary)) == ""


async def test_model_memory_query_requires_target_snapshot(session):
    svc, req = await setup_memory(session)
    with pytest.raises(ValueError, match="memory_snapshot_unbound"):
        await svc.query_memory(
            MemoryQueryRequestV1(
                org_id=req.org_id,
                repository_id=req.repository_id,
                principal_id=req.principal_id,
                query="routing",
            )
        )
