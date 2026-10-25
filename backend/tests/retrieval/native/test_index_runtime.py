"""Opt-in real Linux index preparation with owned synthetic input only."""

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

import pytest
from app.contracts.indexing import IndexBuildConfigV1, IndexFileV1
from app.indexing.adapter import DockerIndexBuilder
from app.indexing.docker import IndexDockerController
from app.ports.index_builder import IndexBuilderUnavailable, VerifiedIndexInputV1
from app.scanners.docker import SandboxLaunch
from app.scanners.ownership import ScannerOwnership
from tests.retrieval.unit.test_index_contracts import spec


def runtime(tmp_path):
    image = os.environ.get("CODESPY_INDEXER_IMAGE")
    source_hash = os.environ.get("CODESPY_INDEXER_SOURCE_HASH")
    if not image or not source_hash:
        pytest.skip("owned pinned indexer image not configured")

    class MeasuredController(IndexDockerController):
        samples: list[int]

        async def start(self, name, maximum):
            self.samples = []
            if maximum <= 65_536:
                return await super().start(name, maximum)
            task = asyncio.create_task(super().start(name, maximum))

            async def sample():
                while not task.done():
                    result = await self._run(
                        ("stats", "--no-stream", "--format", "{{.MemUsage}}", name), seconds=5
                    )
                    match = re.match(rb"([0-9.]+)(B|KiB|MiB|GiB)", result.output.strip())
                    if match:
                        scale = {b"B": 1, b"KiB": 1024, b"MiB": 1048576, b"GiB": 1073741824}
                        self.samples.append(int(float(match[1]) * scale[match[2]]))
                    await asyncio.sleep(0.1)

            sampler = asyncio.create_task(sample())
            try:
                return await task
            finally:
                if not task.done():
                    task.cancel()
                sampler.cancel()
                await asyncio.gather(task, sampler, return_exceptions=True)

    controller = MeasuredController(
        Path("/usr/local/bin/docker"), Path.home() / ".docker/run/docker.sock", tmp_path / "config"
    )
    owner = ScannerOwnership.create(tmp_path / "owner", image)
    verified = tmp_path / "verified"
    verified.mkdir(mode=0o700)
    builder = DockerIndexBuilder(owner, controller, verified, runtime_source_sha256=source_hash)
    return (
        owner,
        controller,
        builder,
        verified,
        IndexBuildConfigV1(runtime_image=image, runtime_source_sha256=source_hash),
    )


def inputs_for(root, contents):
    from uuid import uuid4

    files, inputs = [], []
    for i, content in enumerate(contents):
        path = root / str(i)
        path.write_bytes(content)
        file = IndexFileV1(
            file_id=uuid4(),
            path=f"src/{i:03d}.py",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        files.append(file)
        inputs.append(VerifiedIndexInputV1(file, path))
    return tuple(files), tuple(inputs)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["plain", "long_line", "redactions", "capacity", "metadata"])
async def test_real_offline_index_preparation(tmp_path, scenario):
    owner, controller, builder, verified, config = runtime(tmp_path)
    if scenario == "capacity":
        unit = b"value = 123456789\n"
        content = (unit * (131072 // len(unit) + 1))[:131072]
        contents = [content] * 128
    elif scenario == "metadata":
        contents = [b""] * 128
    elif scenario == "long_line":
        contents = [b"a9_" * 30000]
    elif scenario == "redactions":
        contents = [b"codespy_sec_canary_runtime_123456789\n" * 1000]
    else:
        contents = [b"print(1)\n"]
    files, inputs = inputs_for(verified, contents)
    if scenario == "metadata":
        files = tuple(
            IndexFileV1.model_validate({**f.model_dump(), "path": "x" * 500 + f"{i:03d}.py"})
            for i, f in enumerate(files)
        )
        inputs = tuple(
            VerifiedIndexInputV1(f, item.owned_path) for f, item in zip(files, inputs, strict=True)
        )
    job = spec(files, config=config)
    await builder.validate(config)
    started = time.monotonic()
    try:
        receipt = await builder.build(job, inputs, timeout_seconds=180)
        elapsed = time.monotonic() - started
        receipt.validate()
        assert elapsed < 180
        assert len(receipt.draft.chunks) <= 4096
        assert b"codespy_sec_canary_" not in receipt.draft.model_dump_json().encode()
        assert len(receipt.draft.processed_file_ids) == len(files)
        if scenario == "capacity":
            assert sum(f.size_bytes for f in files) == 16_777_216
            assert controller.samples and max(controller.samples) > 0
            assert max(controller.samples) <= 1_073_741_824
        print(
            json.dumps(
                {
                    "scenario": scenario,
                    "elapsed_seconds": round(elapsed, 3),
                    "chunks": len(receipt.draft.chunks),
                    "coverage": receipt.draft.coverage.status,
                    "memory_limit_bytes": 1073741824,
                    "memory_peak_measured": False,
                    "sampled_max_memory_bytes": max(controller.samples, default=0),
                }
            )
        )
    finally:
        await builder.recover_owned()
    assert not list(owner.root.glob("codespy-scan-*"))


@pytest.mark.asyncio
async def test_real_runtime_cancellation_cleans_owned_container(tmp_path):
    owner, controller, builder, verified, config = runtime(tmp_path)
    started = asyncio.Event()
    original = controller.start

    async def observe(name, maximum):
        if maximum > 65536:
            task = asyncio.create_task(original(name, maximum))
            try:
                while not task.done():
                    state = await controller._run(
                        ("container", "inspect", name, "--format", "{{.State.Running}}")
                    )
                    if state.output.strip() == b"true":
                        started.set()
                        break
                    await asyncio.sleep(0.01)
                return await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return await original(name, maximum)

    controller.start = observe
    content = b"variable9 = 12345\n" * 60000
    files, inputs = inputs_for(verified, [content] * 8)
    job = spec(files, config=config)
    task = asyncio.create_task(builder.build(job, inputs, timeout_seconds=180))
    await asyncio.wait_for(started.wait(), 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await builder.recover_owned()
    assert not list(owner.root.glob("codespy-scan-*"))


@pytest.mark.asyncio
async def test_real_runtime_owner_recovers_created_container_after_crash(tmp_path):
    owner, controller, builder, _, _ = runtime(tmp_path)
    name = owner.reserve()
    root = owner.root / (name + ".input")
    root.mkdir()
    for file in ("archive.zip", "manifest.json", "request.json"):
        (root / file).write_bytes(b"{}")
    try:
        await controller.create(SandboxLaunch(name, owner.marker, owner.image, root))
        restored = ScannerOwnership.restore(owner.root, owner.image)
        await restored.cleanup_pending(controller)
        assert await controller.inspect_owned(name) is None
    finally:
        await builder.recover_owned()
    assert not list(owner.root.glob("codespy-scan-*"))


@pytest.mark.asyncio
async def test_real_runtime_output_overflow_cleans_owned_container(tmp_path):
    owner, controller, builder, verified, config = runtime(tmp_path)
    original = controller.start

    async def bounded(name, maximum):
        return await original(name, min(maximum, 8192))

    controller.start = bounded
    files, inputs = inputs_for(verified, [b"value = 123456789\n" * 10000])
    with pytest.raises(IndexBuilderUnavailable):
        await builder.build(spec(files, config=config), inputs, timeout_seconds=180)
    await builder.recover_owned()
    assert not list(owner.root.glob("codespy-scan-*"))
