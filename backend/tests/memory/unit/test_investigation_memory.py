"""Tool transport tests with an explicitly synthetic memory selection port."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.contracts.context_manifest import MemoryContextSettingsV1
from app.contracts.investigation_execution import InvestigationToolName, InvestigationToolRequestV1
from app.contracts.memory import MemoryEntryV1, MemoryKind, MemoryQueryResponseV1
from app.services.investigation_tools import InvestigationToolService
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.investigations.unit.test_tool_contracts import _make_context


async def test_tool_memory_preserves_versions_and_omits_complete_entries_under_byte_limit():
    context = _make_context()
    now = datetime.now(UTC)
    entries = tuple(
        MemoryEntryV1(
            memory_id=uuid4(),
            org_id=context.scope.org_id,
            repository_id=context.scope.repository_id,
            snapshot_id=context.scope.snapshot_id,
            memory_kind=MemoryKind.ARCHITECTURE_FACT,
            topic="note",
            summary="雪" * 4096,
            state="accepted",
            version=2,
            expires_at=now + timedelta(days=90),
            content_digest="a" * 64,
            created_at=now,
            updated_at=now,
        )
        for _ in range(10)
    )

    class SelectedMemory:
        async def query_memory(self, request):
            assert request.snapshot_id == context.scope.snapshot_id
            assert request.principal_id == context.scope.principal_id
            return MemoryQueryResponseV1(
                org_id=context.scope.org_id,
                repository_id=context.scope.repository_id,
                query=request.query,
                total_candidates=10,
                items=entries,
            )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        service = InvestigationToolService(
            session_factory=async_sessionmaker(engine),
            memory_service=SelectedMemory(),
            memory_context=MemoryContextSettingsV1(mode="reviewed"),
        )
        result = await service.execute(
            InvestigationToolRequestV1(
                tool_name=InvestigationToolName.CHECK_MEMORY,
                arguments={"query": "note", "limit": 10},
            ),
            context,
        )
        parsed = json.loads(result.payload)
        assert result.status == "partial"
        assert len(result.payload.encode()) <= 32768
        assert 0 < len(parsed["entries"]) < 10
        assert parsed["entries"][0]["version"] == 2
        assert parsed["entries"][0]["content_digest"] == "a" * 64
        assert parsed["entries"][0]["source_correspondence"] == "original_snapshot"
    finally:
        await engine.dispose()


async def test_executor_checkpoints_the_gateway_context_reference_without_prompt_text():
    from app.contracts.model import ModelResponseV1
    from app.services.investigation_executor import (
        InMemoryInvestigationAuthority,
        InMemoryInvestigationCheckpointRepo,
        InMemoryInvestigationStepReceiptRepo,
        InvestigationExecutorService,
    )
    from tests.investigations.unit.test_executor import (
        DummyModelGateway,
        DummyToolService,
        _make_context,
        _make_response,
        _make_spec,
    )

    spec = _make_spec()
    context = _make_context(spec)
    response = ModelResponseV1(
        **(
            _make_response("private rendered response").model_dump()
            | {"context_packet_hash": "a" * 64, "memory_provenance_hash": "b" * 64}
        )
    )
    checkpoints = InMemoryInvestigationCheckpointRepo()
    executor = InvestigationExecutorService(
        DummyModelGateway([response]),
        DummyToolService(),
        InMemoryInvestigationAuthority(context.scope),
        checkpoints,
        InMemoryInvestigationStepReceiptRepo(),
        spec_provider=lambda session, org, digest: spec,
    )
    await executor.advance(context)
    await executor.advance(context)
    checkpoint = await checkpoints.load_latest(None, context.scope)
    assert checkpoint.state.context_packet_hash == "a" * 64
    assert checkpoint.state.memory_provenance_hash == "b" * 64
    assert "private rendered response" not in checkpoint.state.model_dump_json()
