"""Worker boundaries use controlled ports; real container execution is qualified separately."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.contracts.jobs import AttemptLeaseV2
from app.indexing.receipt import _issue
from app.workers.operations.index_snapshot import IndexOperation
from tests.retrieval.unit.test_index_contracts import draft, spec


class Context:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *args):
        return None


@pytest.mark.asyncio
async def test_worker_stages_verified_bytes_and_removes_them_before_publication(
    tmp_path, monkeypatch
):
    import app.workers.operations.index_snapshot as module

    selected = spec()
    events = []

    async def current(session, jobs, lease):
        events.append("authority")
        return {"deadline": datetime.now(UTC) + timedelta(minutes=10)}, {}, selected

    async def now(session):
        return datetime.now(UTC)

    monkeypatch.setattr(module, "current_input", current)
    monkeypatch.setattr(module, "database_now", now)
    monkeypatch.setattr(module, "transaction", lambda factory: Context())
    from app.ports.index_builder import IndexCapabilitiesV1

    class Reader:
        async def read(self, request):
            assert request.file_id == selected.files[0].file_id
            events.append("read")
            return b"print(1)\n"

    class Builder:
        async def capabilities(self):
            return IndexCapabilitiesV1(
                runtime_source_sha256=selected.config.runtime_source_sha256, max_request_bytes=98368
            )

        async def build(self, value, inputs, *, timeout_seconds):
            assert 0 < timeout_seconds <= 180
            assert value == selected and inputs[0].owned_path.read_bytes() == b"print(1)\n"
            events.append("build")
            return _issue(value, draft(value))

    class Finalizer:
        async def finish(self, lease, prepared):
            assert list(tmp_path.iterdir()) == []
            events.append("finish")
            return "receipt"

    async def gate():
        events.append("gate")

    lease = AttemptLeaseV2(
        org_id=selected.org_id,
        job_id=uuid4(),
        attempt_id=uuid4(),
        fence=1,
        owner="worker",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=90),
        heartbeat_at=datetime.now(UTC),
    )
    operation = IndexOperation(None, None, Reader(), Builder(), Finalizer(), verified_root=tmp_path)
    assert await operation.execute(lease, enter_finalization=gate) == "receipt"
    assert events == ["authority", "read", "authority", "build", "gate", "finish"]


@pytest.mark.asyncio
async def test_worker_cancellation_joins_read_and_removes_private_staging(tmp_path, monkeypatch):
    import app.workers.operations.index_snapshot as module
    from app.ports.index_builder import IndexCapabilitiesV1

    selected, entered, closed = spec(), asyncio.Event(), asyncio.Event()

    async def current(*args):
        return {"deadline": datetime.now(UTC) + timedelta(minutes=10)}, {}, selected

    async def now(session):
        return datetime.now(UTC)

    monkeypatch.setattr(module, "current_input", current)
    monkeypatch.setattr(module, "database_now", now)
    monkeypatch.setattr(module, "transaction", lambda factory: Context())

    class Reader:
        async def read(self, request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

    class Builder:
        async def capabilities(self):
            return IndexCapabilitiesV1(
                runtime_source_sha256=selected.config.runtime_source_sha256, max_request_bytes=98368
            )

        async def build(self, *args, **kwargs):
            raise AssertionError("cancelled input must not build")

    operation = IndexOperation(None, None, Reader(), Builder(), None, verified_root=tmp_path)
    task = asyncio.create_task(operation.execute(None))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set() and list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_runtime_recovers_owned_index_children_and_joins_shutdown():
    from app.workers.runtime import WorkerRuntime

    started, closed, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Builder:
        async def recover_owned(self):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

    class Operation:
        async def recover_owned(self):
            await Builder().recover_owned()

    class Queue:
        async def receive(self, *args, **kwargs):
            await asyncio.Event().wait()

    class Consumer:
        queue = Queue()
        index_operation = Operation()

    class Dispatcher:
        async def dispatch_once(self):
            return 0

    async def noop():
        return 0

    task = asyncio.create_task(WorkerRuntime(Dispatcher(), Consumer()).run(stop, noop, noop))
    async with asyncio.timeout(2):
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed.is_set()


@pytest.mark.asyncio
async def test_durable_recovery_reports_unknown_when_database_cannot_be_read():
    from app.domain.errors import FinalizationOutcomeUnknown
    from app.services.index_publication import IndexFinalizer

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def begin(self):
            return self

        async def execute(self, *args):
            raise OSError("synthetic unavailable database")

        async def rollback(self):
            pass

        async def close(self):
            pass

    with pytest.raises(FinalizationOutcomeUnknown):
        await IndexFinalizer(lambda: Session())._durable(None, "0" * 64)
