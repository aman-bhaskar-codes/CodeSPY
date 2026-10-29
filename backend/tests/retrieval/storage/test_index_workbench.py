"""Owned browser journey: trusted fixture admission, real isolated indexing and PostgreSQL."""

import asyncio
import io
import json
import os
import socket
import zipfile
from pathlib import Path

import httpx
import pytest
from app.adapters.queue.memory import MemoryQueue
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.contracts.indexing import IndexBuildConfigV1
from app.indexing.adapter import DockerIndexBuilder
from app.indexing.docker import IndexDockerController
from app.scanners.docker import DockerController
from app.scanners.ownership import ScannerOwnership
from app.services.index_publication import IndexFinalizer
from app.services.job_finalization import JobFinalizer
from app.source_read.adapter import DockerSourceReader
from app.workers.cleanup import CleanupWorker
from app.workers.consumer import JobConsumer
from app.workers.dispatcher import OutboxDispatcher
from app.workers.operations.index_snapshot import IndexOperation
from app.workers.recovery import RecoveryWorker
from app.workers.runtime import WorkerRuntime

from tests.phase2.integration import test_submission_ingestion as ingestion
from tests.phase3.integration.test_structural_publication import admitted

ROOT = Path(__file__).resolve().parents[4]


class OneIndexQueue(MemoryQueue):
    """Deliver the first real build, then hold subsequent work for queued cancellation."""

    def __init__(self):
        super().__init__()
        self.index_delivered = False
        self.release = asyncio.Event()

    async def receive(self, max_messages=1, *, wait_seconds=20):
        if self.index_delivered:
            await self.release.wait()
        deliveries = await super().receive(1, wait_seconds=wait_seconds)
        if any(json.loads(item.body).get("operation") == "index_snapshot" for item in deliveries):
            self.index_delivered = True
        return deliveries


class NoAdmission:
    async def inspect(self, *_args):
        raise AssertionError("The owned archive was already admitted by the trusted fixture")


def bind_loopback(port):
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen()
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


async def ready(url, process=None):
    async with asyncio.timeout(30), httpx.AsyncClient() as client:
        while True:
            if process is not None and process.returncode is not None:
                raise AssertionError("Owned frontend process exited before readiness")
            try:
                if (await client.get(url)).status_code < 500:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.05)


async def stop_process(process):
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        async with asyncio.timeout(5):
            await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()


async def test_real_index_workbench_browser(
    api_factory, broker_factory, migration_factory, tmp_path, monkeypatch
):
    if os.environ.get("CODESPY_RUN_INDEX_WORKBENCH") != "1":
        pytest.skip("explicit owned browser qualification required")
    import uvicorn

    image, source_hash, reader_image = (
        os.environ.get(key)
        for key in (
            "CODESPY_INDEXER_IMAGE",
            "CODESPY_INDEXER_SOURCE_HASH",
            "CODESPY_SOURCE_READER_IMAGE",
        )
    )
    if not all((image, source_hash, reader_image)):
        pytest.skip("owned qualified reader/indexer images required")
    frontend = ROOT / "frontend"
    import shutil

    node_binary = os.environ.get("CODESPY_TEST_NODE_BINARY") or shutil.which("node")
    assert node_binary, "Node runtime required for owned browser tests"
    node_dir = Path(node_binary).parent
    cli = frontend / "node_modules/@playwright/test/cli.js"
    assert cli.is_file(), "Restore the project's locked Node test dependencies first"
    archive = io.BytesIO()
    canary = "codespy_sec_canary_browser_123456789"
    with zipfile.ZipFile(archive, "w") as body:
        body.writestr("a.py", f"token = '{canary}'\nprint('browser_index_marker')\n")
    monkeypatch.setattr(ingestion, "zip_bytes", archive.getvalue)
    ctx, snapshot, repo, store = await admitted(migration_factory, tmp_path)
    docker_socket = Path.home() / ".docker/run/docker.sock"
    reader = DockerSourceReader(
        ScannerOwnership.create(tmp_path / "reader-owner", reader_image),
        DockerController(Path("/usr/local/bin/docker"), docker_socket, tmp_path / "reader-config"),
        store,
    )
    verified = tmp_path / "verified"
    verified.mkdir(mode=0o700)
    builder = DockerIndexBuilder(
        ScannerOwnership.create(tmp_path / "index-owner", image),
        IndexDockerController(
            Path("/usr/local/bin/docker"), docker_socket, tmp_path / "index-config"
        ),
        verified,
        runtime_source_sha256=source_hash,
    )
    config = IndexBuildConfigV1(runtime_image=image, runtime_source_sha256=source_hash)
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://127.0.0.1:5173"),
        ApplicationPorts(
            api_factory,
            store,
            ctx,
            source_reader=reader,
            index_config=config,
            index_builder=builder,
        ),
    )
    queue = OneIndexQueue()
    operation = IndexOperation(
        broker_factory,
        store,
        reader,
        builder,
        IndexFinalizer(broker_factory),
        verified_root=verified,
    )
    runtime = WorkerRuntime(
        OutboxDispatcher(broker_factory, queue),
        JobConsumer(
            broker_factory,
            queue,
            JobFinalizer(broker_factory, store),
            NoAdmission(),
            index_operation=operation,
        ),
    )
    stop = asyncio.Event()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8000, access_log=False))
    server_task = worker = vite = browser = None
    api_socket = None
    log = (tmp_path / "browser-processes.log").open("wb")
    try:
        api_socket = bind_loopback(8000)
        frontend_reservation = bind_loopback(5173)
        frontend_reservation.close()
        worker = asyncio.create_task(
            runtime.run(
                stop, RecoveryWorker(broker_factory).tick, CleanupWorker(broker_factory, store).tick
            )
        )
        server_task = asyncio.create_task(server.serve(sockets=[api_socket]))
        await ready("http://127.0.0.1:8000/api/v1/session")
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("AWS_", "OPENAI_", "ANTHROPIC_", "GEMINI_"))
        }
        environment.update(
            PATH=str(node_dir) + ":" + environment.get("PATH", ""),
            CODESPY_E2E_REPOSITORY=str(repo),
            CODESPY_E2E_SNAPSHOT=str(snapshot),
            CODESPY_E2E_CANARY=canary,
        )
        vite_config = tmp_path / "vite.config.mjs"
        vite_config.write_text(
            "import base from "
            + json.dumps((frontend / "vite.config.ts").as_uri())
            + "; export default {...base, root: "
            + json.dumps(str(frontend))
            + ", envDir: false};\n"
        )
        vite = await asyncio.create_subprocess_exec(
            str(node_dir / "node"),
            str(frontend / "node_modules/vite/bin/vite.js"),
            "--host",
            "127.0.0.1",
            "--port",
            "5173",
            "--strictPort",
            "--config",
            str(vite_config),
            cwd=frontend,
            env=environment,
            stdout=log,
            stderr=log,
        )
        await ready("http://127.0.0.1:5173", vite)
        browser = await asyncio.create_subprocess_exec(
            str(node_dir / "node"),
            str(cli),
            "test",
            "indexing.spec.ts",
            "--workers=1",
            "--reporter=line",
            cwd=frontend,
            env=environment,
            stdout=log,
            stderr=log,
        )
        async with asyncio.timeout(180):
            code = await browser.wait()
        log.flush()
        assert code == 0, (tmp_path / "browser-processes.log").read_text()[-20000:]
    finally:
        await stop_process(browser)
        await stop_process(vite)
        server.should_exit = True
        stop.set()
        queue.release.set()
        for task in (server_task, worker):
            if task is not None:
                try:
                    async with asyncio.timeout(10):
                        await task
                except TimeoutError:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        if api_socket is not None:
            api_socket.close()
        await builder.recover_owned()
        await reader.recover_owned()
        log.close()
