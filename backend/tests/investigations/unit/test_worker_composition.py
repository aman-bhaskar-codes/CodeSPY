"""Attempt-bound composition must not mutate a shared provider gateway."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from app.contracts.investigation_execution import (
    InvestigationAdvanceStatus,
    InvestigationAdvanceV1,
    InvestigationExecutionContextV1,
)
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.model_configuration import ModelRegistryConfigV1
from app.contracts.provider_budget import DisclosurePolicyV1
from app.services.investigation_runtime import build_investigation_operation
from app.services.model_continuations import ContinuationCipher, ModelContinuationService
from app.services.model_registry import ModelRegistry
from app.services.model_runtime import ConfiguredModelRuntime
from app.workers.composition import build_worker_runtime
from app.workers.operations.investigate_snapshot import InvestigateSnapshotOperation
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.investigations.unit.test_executor import DummyToolService, _make_context, _make_spec


def test_runtime_forwards_explicit_result_and_authority_ports() -> None:
    runtime = ConfiguredModelRuntime(
        ModelRegistry(
            ModelRegistryConfigV1(), registered_providers=frozenset(), active_prices=frozenset()
        ),
        {},
        async_sessionmaker(),
    )
    store, authority = AsyncMock(), AsyncMock()
    gateway = runtime.gateway(result_store=store, dispatch_authority=authority)
    assert gateway.result_store is store
    assert gateway.dispatch_authority is authority


def test_worker_factory_binds_distinct_gateways_to_each_attempt() -> None:
    factory = async_sessionmaker()
    runtime = ConfiguredModelRuntime(
        ModelRegistry(
            ModelRegistryConfigV1(), registered_providers=frozenset(), active_prices=frozenset()
        ),
        {},
        factory,
    )
    operation = build_investigation_operation(
        runtime=runtime, worker_factory=factory, tools=DummyToolService(), result_store=AsyncMock()
    )
    ctx = _make_context(_make_spec())
    lease = AttemptLeaseV2(
        org_id=ctx.scope.org_id,
        job_id=ctx.attempt_job_id,
        attempt_id=ctx.attempt_id,
        fence=ctx.attempt_fence,
        owner="worker-a",
        lease_expires_at=ctx.deadline,
        heartbeat_at=ctx.deadline,
    )
    other = lease.model_copy(update={"fence": lease.fence + 1})
    assert operation.executor_factory is not None
    first = operation.executor_factory(lease)
    second = operation.executor_factory(other)
    assert operation.finalizer.source_support is not None
    assert first.gateway.dispatch_authority.support is not None
    assert first.gateway is not second.gateway
    assert first.bound_lease == lease
    assert second.bound_lease == other
    assert first.gateway.dispatch_authority.lease == lease
    assert second.gateway.dispatch_authority.lease == other


def test_worker_startup_connects_investigation_and_maintenance_without_credentials() -> None:
    factory = async_sessionmaker()
    runtime = ConfiguredModelRuntime(
        ModelRegistry(
            ModelRegistryConfigV1(), registered_providers=frozenset(), active_prices=frozenset()
        ),
        {},
        factory,
        continuations=ModelContinuationService(ContinuationCipher(bytes(range(32)))),
    )
    worker = build_worker_runtime(
        factory=factory,
        queue=AsyncMock(),
        store=AsyncMock(),
        admission=AsyncMock(),
        model_runtime=runtime,
    )
    assert worker.consumer.investigation_operation is not None
    assert worker.model_maintenance == runtime.maintenance_tick
    assert worker.consumer.investigation_operation.finalizer.source_support is not None


def test_investigation_gateway_budgets_request_even_when_memory_is_disabled() -> None:
    factory = async_sessionmaker()
    runtime = ConfiguredModelRuntime(
        ModelRegistry(
            ModelRegistryConfigV1(), registered_providers=frozenset(), active_prices=frozenset()
        ),
        {},
        factory,
        continuations=ModelContinuationService(ContinuationCipher(bytes(range(32)))),
    )
    operation = build_investigation_operation(
        runtime=runtime, worker_factory=factory, tools=DummyToolService(), result_store=AsyncMock()
    )
    ctx = _make_context(_make_spec())
    lease = AttemptLeaseV2(
        org_id=ctx.scope.org_id,
        job_id=ctx.attempt_job_id,
        attempt_id=ctx.attempt_id,
        fence=ctx.attempt_fence,
        owner="worker",
        lease_expires_at=ctx.deadline,
        heartbeat_at=ctx.deadline,
    )
    assert operation.executor_factory is not None
    assert operation.executor_factory(lease).gateway.context_limits == runtime.memory_context.limits


def test_worker_startup_refuses_missing_continuations_storage() -> None:
    factory = async_sessionmaker()
    runtime = ConfiguredModelRuntime(
        ModelRegistry(
            ModelRegistryConfigV1(), registered_providers=frozenset(), active_prices=frozenset()
        ),
        {},
        factory,
        continuations=None,
    )
    with pytest.raises(
        ValueError, match="investigation startup requires encrypted continuation storage"
    ):
        build_worker_runtime(
            factory=factory,
            queue=AsyncMock(),
            store=AsyncMock(),
            admission=AsyncMock(),
            model_runtime=runtime,
        )


def test_worker_startup_refuses_proposals_without_model_runtime() -> None:
    factory = async_sessionmaker()
    with pytest.raises(ValueError, match="report proposals require a configured model runtime"):
        build_worker_runtime(
            factory=factory,
            queue=AsyncMock(),
            store=AsyncMock(),
            admission=AsyncMock(),
            model_runtime=None,
            propose_report_memory=True,
        )


def test_investigation_operation_refuses_proposals_without_source_reader() -> None:
    factory = async_sessionmaker()
    runtime = ConfiguredModelRuntime(
        ModelRegistry(
            ModelRegistryConfigV1(), registered_providers=frozenset(), active_prices=frozenset()
        ),
        {},
        factory,
        source_reader=None,
        continuations=ModelContinuationService(ContinuationCipher(bytes(range(32)))),
    )
    with pytest.raises(
        ValueError, match="report memory proposals require an isolated source reader"
    ):
        build_investigation_operation(
            runtime=runtime,
            worker_factory=factory,
            tools=DummyToolService(),
            result_store=AsyncMock(),
            propose_report_memory=True,
        )


def test_investigation_operation_resolves_server_policy() -> None:
    factory = async_sessionmaker()
    custom_policy = DisclosurePolicyV1(allow_external_disclosure=True, allowed_providers=("fake",))
    ctx = _make_context(_make_spec())
    runtime = ConfiguredModelRuntime(
        ModelRegistry(
            ModelRegistryConfigV1(), registered_providers=frozenset(), active_prices=frozenset()
        ),
        {},
        factory,
        disclosure_policies={(ctx.scope.org_id, ctx.scope.repository_id): custom_policy},
        continuations=ModelContinuationService(ContinuationCipher(bytes(range(32)))),
    )
    operation = build_investigation_operation(
        runtime=runtime,
        worker_factory=factory,
        tools=DummyToolService(),
        result_store=AsyncMock(),
    )
    lease = AttemptLeaseV2(
        org_id=ctx.scope.org_id,
        job_id=ctx.attempt_job_id,
        attempt_id=ctx.attempt_id,
        fence=ctx.attempt_fence,
        owner="worker",
        lease_expires_at=ctx.deadline,
        heartbeat_at=ctx.deadline,
    )
    assert operation.executor_factory is not None
    executor = operation.executor_factory(lease)
    resolved = executor.policy_resolver(ctx.scope)
    assert resolved == custom_policy
    assert resolved.allow_external_disclosure is True


@pytest.mark.asyncio
async def test_investigate_snapshot_operation_preserves_optional_scope_identities() -> None:
    finding_id = uuid4()
    finding_run_id = uuid4()
    chunk_set_id = uuid4()
    spec = _make_spec().model_copy(
        update={
            "finding_id": finding_id,
            "finding_run_id": finding_run_id,
            "chunk_set_id": chunk_set_id,
        }
    )
    ctx = _make_context(spec)
    lease = AttemptLeaseV2(
        org_id=ctx.scope.org_id,
        job_id=ctx.attempt_job_id,
        attempt_id=ctx.attempt_id,
        fence=ctx.attempt_fence,
        owner="worker-1",
        lease_expires_at=ctx.deadline,
        heartbeat_at=ctx.deadline,
    )
    job_record = {
        "operation": "investigate_snapshot",
        "investigation_id": ctx.scope.investigation_id,
        "spec_hash": spec.compute_spec_hash(),
        "deadline": ctx.deadline,
        "repository_id": ctx.scope.repository_id,
        "principal_id": ctx.scope.principal_id,
    }

    mock_spec_repo = AsyncMock()
    mock_spec_repo.get_spec = AsyncMock(return_value=spec)

    captured_ctx: list[InvestigationExecutionContextV1] = []

    async def fake_advance(context: InvestigationExecutionContextV1) -> InvestigationAdvanceV1:
        captured_ctx.append(context)
        return InvestigationAdvanceV1(
            status=InvestigationAdvanceStatus.READY_TO_PUBLISH,
            checkpoint_sequence=1,
        )

    mock_executor = AsyncMock()
    mock_executor.advance = AsyncMock(side_effect=fake_advance)

    mock_session = AsyncMock()
    mock_factory = async_sessionmaker()

    op = InvestigateSnapshotOperation(
        mock_factory,
        executor=mock_executor,
        spec_repo=mock_spec_repo,
    )

    with (
        patch("app.workers.operations.investigate_snapshot.transaction") as mock_tx,
        patch(
            "app.workers.operations.investigate_snapshot.load_job",
            AsyncMock(return_value=job_record),
        ),
    ):
        mock_tx.return_value.__aenter__.return_value = mock_session
        result = await op.execute(lease)

    assert result.status == InvestigationAdvanceStatus.READY_TO_PUBLISH
    assert len(captured_ctx) == 1
    assert captured_ctx[0].scope.finding_id == finding_id
    assert captured_ctx[0].scope.finding_run_id == finding_run_id
    assert captured_ctx[0].scope.chunk_set_id == chunk_set_id
    assert captured_ctx[0].scope.snapshot_id == spec.snapshot_id
