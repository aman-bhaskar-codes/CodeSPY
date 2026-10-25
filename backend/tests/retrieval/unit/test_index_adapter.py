"""Controlled controller tests; these do not qualify real Docker isolation."""

import asyncio
import os
from pathlib import Path

import pytest
from app.scanners.ownership import ScannerOwnership
from app.scanners.process import NativeResult
from tests.retrieval.unit.test_index_contracts import file, spec


class ControlledController:
    def __init__(self, job, scenario="valid"):
        self.job, self.scenario = job, scenario
        self.created = {}
        self.started = asyncio.Event()
        self.removed = []

    async def image_entrypoint(self, image):
        return (
            ("python", "-m", "app.indexing.entrypoint") if self.scenario != "image" else ("other",)
        )

    async def create(self, launch):
        self.created[launch.name] = launch
        if self.scenario == "crash_create" or (
            self.scenario == "crash_build"
            and b'"build"' in (launch.input_root / "request.json").read_bytes()
        ):
            raise OSError("uncertain create")

    async def inspect_owned(self, name):
        launch = self.created.get(name)
        return (
            None
            if launch is None
            else {"image": launch.image, "marker": launch.marker, "id": "d" * 64}
        )

    async def remove(self, identity):
        self.removed.append(identity)
        self.created.clear()

    async def verify_controls(self, name):
        assert name in self.created

    async def start(self, name, maximum):
        from app.indexing.entrypoint import execute

        launch = self.created[name]
        request = (launch.input_root / "request.json").read_bytes()
        if b'"capabilities"' not in request:
            self.started.set()
            if self.scenario == "wait":
                await asyncio.Event().wait()
        result = execute(
            launch.input_root,
            self.job.config.runtime_source_sha256,
            Path(os.environ["TIKTOKEN_CACHE_DIR"]),
        )
        if b'"capabilities"' not in request:
            if self.scenario == "malformed":
                result = b"not json"
            if self.scenario == "overflow":
                result = b"x" * (25_165_824 + 1)
            if self.scenario == "wrong_spec":
                result = result.replace(self.job.semantic_hash().encode(), b"0" * 64)
        return NativeResult(0, result, 1 if self.scenario == "diagnostic" else 0)


def setup(tmp_path, scenario="valid"):
    from app.indexing.adapter import DockerIndexBuilder
    from app.ports.index_builder import VerifiedIndexInputV1

    raw = b"print(1)\n"
    job = spec((file(raw=raw),))
    owner = ScannerOwnership.create(tmp_path / "owner", job.config.runtime_image)
    inputs = tmp_path / "verified"
    inputs.mkdir(mode=0o700)
    path = inputs / "source"
    path.write_bytes(raw)
    controller = ControlledController(job, scenario)
    builder = DockerIndexBuilder(
        owner, controller, inputs, runtime_source_sha256=job.config.runtime_source_sha256
    )
    return job, owner, controller, builder, (VerifiedIndexInputV1(job.files[0], path),)


@pytest.mark.asyncio
async def test_valid_build_issues_bound_receipt_and_cleans_private_input(tmp_path):
    job, owner, controller, builder, inputs = setup(tmp_path)
    receipt = await builder.build(job, inputs, timeout_seconds=5)
    receipt.validate()
    assert receipt.spec_hash == job.semantic_hash()
    assert receipt.draft_digest == receipt.draft.semantic_hash()
    assert receipt.image == job.config.runtime_image
    assert len(receipt.draft.chunks) == 1
    assert not controller.created
    assert not list(owner.root.glob("codespy-scan-*"))
    assert inputs[0].owned_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["image", "crash_create", "malformed", "overflow", "wrong_spec", "diagnostic"]
)
async def test_failed_build_is_unavailable_and_cleans_owned_resources(tmp_path, scenario):
    from app.ports.index_builder import IndexBuilderUnavailable

    job, owner, controller, builder, inputs = setup(tmp_path, scenario)
    with pytest.raises(IndexBuilderUnavailable):
        await builder.build(job, inputs, timeout_seconds=5)
    assert not controller.created
    assert not list(owner.root.glob("codespy-scan-*"))


@pytest.mark.asyncio
async def test_cancellation_remains_cancellation_and_removes_container(tmp_path):
    job, owner, controller, builder, inputs = setup(tmp_path, "wait")
    task = asyncio.create_task(builder.build(job, inputs, timeout_seconds=5))
    await asyncio.wait_for(controller.started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not controller.created
    assert not list(owner.root.glob("codespy-scan-*"))


@pytest.mark.asyncio
async def test_total_deadline_includes_runtime_and_cleanup_is_durable(tmp_path):
    from app.ports.index_builder import IndexBuilderUnavailable

    job, owner, controller, builder, inputs = setup(tmp_path, "wait")
    with pytest.raises(IndexBuilderUnavailable):
        await builder.build(job, inputs, timeout_seconds=0.05)
    assert not controller.created
    assert not list(owner.root.glob("codespy-scan-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["foreign", "traversal", "symlink", "hash", "missing", "duplicate"]
)
async def test_only_exact_verified_owned_regular_files_are_staged(tmp_path, fault):
    from app.ports.index_builder import IndexBuilderUnavailable, VerifiedIndexInputV1

    job, owner, _, builder, inputs = setup(tmp_path)
    original = inputs[0]
    if fault == "foreign":
        path = tmp_path / "outside"
        path.write_bytes(b"print(1)\n")
        inputs = (VerifiedIndexInputV1(original.file, path),)
    elif fault == "traversal":
        path = tmp_path / "outside"
        path.write_bytes(b"print(1)\n")
        inputs = (
            VerifiedIndexInputV1(original.file, original.owned_path.parent / ".." / "outside"),
        )
    elif fault == "symlink":
        original.owned_path.unlink()
        original.owned_path.symlink_to(tmp_path / "outside")
    elif fault == "hash":
        original.owned_path.write_bytes(b"different")
    elif fault == "missing":
        inputs = ()
    else:
        inputs = inputs + inputs
    with pytest.raises(IndexBuilderUnavailable):
        await builder.build(job, inputs, timeout_seconds=5)
    assert not list(owner.root.glob("codespy-scan-*"))


@pytest.mark.asyncio
async def test_receipt_rejects_mutated_digest_and_has_no_source_repr(tmp_path):

    job, _, _, builder, inputs = setup(tmp_path)
    receipt = await builder.build(job, inputs, timeout_seconds=5)
    object.__setattr__(receipt, "draft_digest", "0" * 64)
    with pytest.raises(ValueError, match="identity differs"):
        receipt.validate()
    assert "print(1)" not in repr(receipt)


@pytest.mark.asyncio
async def test_issuer_is_required_even_for_valid_dto(tmp_path):
    from app.indexing.receipt import PreparedIndexReceipt

    value = object.__new__(PreparedIndexReceipt)
    with pytest.raises(ValueError, match="receipt required"):
        value.validate()


@pytest.mark.asyncio
async def test_cleanup_failure_retains_ledger_for_exclusive_recovery(tmp_path):
    from app.ports.index_builder import IndexBuilderUnavailable

    job, owner, controller, builder, inputs = setup(tmp_path, "crash_create")
    remove = controller.remove

    async def unavailable(identity):
        raise OSError("Docker unavailable")

    controller.remove = unavailable
    with pytest.raises(IndexBuilderUnavailable):
        await builder.build(job, inputs, timeout_seconds=5)
    assert list(owner.root.glob("codespy-scan-*.json"))
    controller.remove = remove
    await builder.recover_owned()
    assert not list(owner.root.glob("codespy-scan-*"))


@pytest.mark.asyncio
async def test_non_yielding_receipt_work_cannot_return_success_past_deadline(tmp_path, monkeypatch):
    import time

    from app.indexing import adapter
    from app.ports.index_builder import IndexBuilderUnavailable

    job, _, _, builder, inputs = setup(tmp_path)
    await builder.capabilities()
    called = []
    issue = adapter._issue

    def delayed(specification, draft):
        # Deliberate non-cooperative work lets the deadline expire without a task switch.
        called.append(True)
        time.sleep(0.6)
        return issue(specification, draft)

    monkeypatch.setattr(adapter, "_issue", delayed)
    with pytest.raises(IndexBuilderUnavailable):
        await builder.build(job, inputs, timeout_seconds=0.5)
    assert called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,code",
    [
        ("wrong_spec", "integrity_failure"),
        ("malformed", "integrity_failure"),
        ("wait", "index_timeout"),
        ("crash_build", "index_process_failed"),
    ],
)
async def test_build_failure_classification_preserves_retry_boundary(tmp_path, scenario, code):
    from app.ports.index_builder import IndexBuilderUnavailable

    job, _, _, builder, inputs = setup(tmp_path, scenario)
    with pytest.raises(IndexBuilderUnavailable) as caught:
        await builder.build(job, inputs, timeout_seconds=0.1 if scenario == "wait" else 5)
    assert caught.value.failure_code == code
