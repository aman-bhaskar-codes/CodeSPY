"""Unit tests for StructuralAnalysisService, StructuralFinalizer, and PublicationExecutor."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.structure import (
    StructuralCoverageV1,
    StructuralDraftV1,
)
from app.services.finalization_budget import FinalizationBudget
from app.services.publication import PublicationExecutor
from app.services.structural_finalization import StructuralFinalizer
from sqlalchemy.ext.asyncio import AsyncSession


class MockRawConn:
    def __init__(self):
        self.is_valid = True
        self.driver_connection = self

    def terminate(self):
        self.is_valid = False

    def invalidate(self, soft=True):
        self.is_valid = False

    def detach(self):
        pass


class MockConn:
    async def get_raw_connection(self):
        return MockRawConn()


class MockSession:
    def __init__(self):
        self.committed = False
        self.rolled_back = False

    async def begin(self):
        pass

    async def connection(self):
        return MockConn()

    async def execute(self, statement, params=None):
        pass

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def close(self):
        pass


def mock_factory(session: MockSession):
    def factory():
        return session

    return factory


@pytest.mark.asyncio
async def test_publication_executor_clean_run():
    session = MockSession()
    executor = PublicationExecutor(mock_factory(session), budget_seconds=10.0)

    async def work(s: AsyncSession, budget: FinalizationBudget) -> str:
        return "success"

    async def resolve() -> str:
        return "resolved"

    res = await executor.execute(work, resolve)
    assert res == "success"
    assert session.committed is True


@pytest.mark.asyncio
async def test_publication_executor_post_commit_recovery():
    session = MockSession()

    async def failing_commit(s: AsyncSession) -> None:
        await s.commit()
        raise ConnectionResetError("network lost after commit")

    executor = PublicationExecutor(
        mock_factory(session), budget_seconds=10.0, commit_hook=failing_commit
    )

    async def work(s: AsyncSession, budget: FinalizationBudget) -> str:
        return "work-done"

    async def resolve() -> str:
        return "recovered-from-db"

    res = await executor.execute(work, resolve)
    assert res == "recovered-from-db"


@pytest.mark.asyncio
async def test_structural_finalizer_requires_run_ref():
    finalizer = StructuralFinalizer(mock_factory(MockSession()), None)  # type: ignore
    lease = AttemptLeaseV2(
        org_id=uuid4(),
        job_id=uuid4(),
        attempt_id=uuid4(),
        fence=1,
        owner="worker-1",
        lease_expires_at=datetime.now(UTC),
        heartbeat_at=datetime.now(UTC),
        run_ref=None,
    )
    draft = StructuralDraftV1(
        request_nonce="nonce-1",
        config_hash="0" * 64,
        toolchain_hash="1" * 64,
        archive_sha256="2" * 64,
        policy_hash="3" * 64,
        coverage=StructuralCoverageV1(
            requested_files=0,
            processed_files=0,
            omitted_files=0,
            resolved=0,
            ambiguous=0,
            unresolved=0,
            syntax="complete",
            resolution="complete",
        ),
    )
    with pytest.raises(ValueError, match="analysis finalization requires run_ref"):
        await finalizer.finish_analysis(lease, draft)
