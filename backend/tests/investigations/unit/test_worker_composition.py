"""Attempt-bound composition must not mutate a shared provider gateway."""

from unittest.mock import AsyncMock

from app.contracts.jobs import AttemptLeaseV2
from app.contracts.model_configuration import ModelRegistryConfigV1
from app.services.model_registry import ModelRegistry
from app.services.model_runtime import ConfiguredModelRuntime
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.investigations.unit.test_executor import DummyToolService, _make_context, _make_spec


def test_runtime_forwards_explicit_result_and_authority_ports():
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


def test_worker_factory_binds_distinct_gateways_to_each_attempt():
    from app.services.investigation_runtime import build_investigation_operation

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
    first = operation.executor_factory(lease)
    second = operation.executor_factory(other)
    assert operation.finalizer.source_support is not None
    assert first.gateway.dispatch_authority.support is not None
    assert first.gateway is not second.gateway
    assert first.bound_lease == lease
    assert second.bound_lease == other
    assert first.gateway.dispatch_authority.lease == lease
    assert second.gateway.dispatch_authority.lease == other
