"""Real Linux native tools through admitted source, queue and durable database reads."""

import json
from pathlib import Path
from uuid import uuid4

from app.adapters.queue.memory import MemoryQueue
from app.contracts.dependencies import DependencyFileV1
from app.contracts.scan_jobs import DependencySelectionV1, NativeSelectionV1, ScanConfigV1
from app.contracts.scanners import ScannerFileV1
from app.scanners.docker import DockerController, SandboxLaunch
from app.scanners.finding_catalog import reviewed_catalog
from app.scanners.ownership import ScannerOwnership
from app.scanners.runtime import RuntimeManifest
from app.services.analysis_sources import locate_source
from app.services.job_finalization import JobFinalizer
from app.services.jobs import JobService
from app.services.scan_capabilities import LocalScanCapabilities
from app.services.scan_jobs import ScanJobService
from app.services.scan_publication import FindingFinalizer
from app.services.scan_reads import ScanReadService
from app.workers.consumer import JobConsumer
from app.workers.dispatcher import OutboxDispatcher
from app.workers.operations.scan_snapshot import ScanOperation
from tests.phase4.process.test_scanner_image import settings
from tests.phase4.storage.scan_fixtures import admit_contents
from tests.phase4.unit.test_findings import matched_bundle
from tests.phase4.unit.test_scanner_archive import fixture_input


async def test_real_queue_native_dependencies_publication_and_authorized_read(
    api_factory, broker_factory, migration_factory, tmp_path
):
    binary, socket, image = settings()
    controller = DockerController(Path(binary), Path(socket), tmp_path / "docker-config")
    owner = ScannerOwnership.create(tmp_path / "probe-owner", image)
    name = owner.reserve()
    root = owner.root / (name + ".input")
    root.mkdir()
    source = fixture_input(root)
    (root / "request.json").write_text(source.model_dump_json())
    try:
        await controller.create(SandboxLaunch(name, owner.marker, image, root, mode="identities"))
        native = await controller.start(name, 65536)
        manifest = RuntimeManifest.model_validate_json(native.output)
    finally:
        await owner.remove(name, controller)
    canary = b"ghp_" + b"A" * 36
    contents = {
        "unsafe.py": b'import subprocess\nsubprocess.run("example", shell=True)\n',
        "safe.py": b'print("fixture")\n',
        "token.py": b'token = "' + canary + b'"\n',
        "package-lock.json": json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "fixture"},
                    "node_modules/example": {
                        "version": "1.2.3",
                        "resolved": "https://registry.npmjs.org/example/-/example-1.2.3.tgz",
                    },
                },
            }
        ).encode(),
    }
    ctx, repo, snapshot, store = await admit_contents(migration_factory, tmp_path, contents)
    async with broker_factory() as session:
        located = await locate_source(session, ctx.org_id, repo, snapshot)
    profiles = tuple(t.profile() for t in manifest.tools)
    catalog = reviewed_catalog(profiles)
    bundle = matched_bundle()
    selections = tuple(
        NativeSelectionV1(
            profile=p,
            files=tuple(
                ScannerFileV1(file_id=f.file_id, sha256=f.sha256, size_bytes=f.size_bytes)
                for f in located.files
                if f.eligible and f.language == "python"
            ),
        )
        for p in profiles
    )
    lock = next(f for f in located.files if f.path == "package-lock.json")
    config = ScanConfigV1(
        runtime_digest=image.removeprefix("sha256:"),
        catalog_digest=catalog.semantic_hash(),
        native_selections=selections,
        dependency_selection=DependencySelectionV1(
            bundle_digest=bundle.digest(),
            files=(
                DependencyFileV1(
                    file_id=lock.file_id,
                    sha256=lock.sha256,
                    size_bytes=lock.size_bytes,
                    format="package-lock-v3",
                ),
            ),
        ),
    )
    owners = []

    def ownership():
        value = ScannerOwnership.create(tmp_path / ("owner-" + uuid4().hex), image)
        owners.append(value)
        return value

    capabilities = LocalScanCapabilities(
        manifest,
        image,
        {catalog.semantic_hash(): catalog},
        {bundle.digest(): bundle},
        store,
        controller,
        ownership,
        await controller.image_entrypoint(image),
    )
    _, job = await ScanJobService(api_factory, capabilities).create(
        ctx, repo, snapshot, config, "native"
    )
    queue = MemoryQueue()
    operation = ScanOperation(broker_factory, store, capabilities, FindingFinalizer(broker_factory))
    consumer = JobConsumer(
        broker_factory, queue, JobFinalizer(broker_factory, store), None, scan_operation=operation
    )
    try:
        await OutboxDispatcher(broker_factory, queue).dispatch_once()
        while deliveries := await queue.receive(2, wait_seconds=0):
            for delivery in deliveries:
                await consumer.handle(delivery)
        stored = await JobService(broker_factory).get(ctx, job.id)
        assert stored.state == "succeeded" and stored.report_available
        from app.domain.scope import RunRef

        result = await ScanReadService(api_factory, capabilities).read(
            ctx, RunRef(org_id=ctx.org_id, snapshot_id=snapshot, run_id=stored.result_run_id)
        )
        assert result.historical.coverage == "complete"
        assert len(result.historical.findings) == 3
        assert {f.category for f in result.historical.findings} == {
            "source_pattern",
            "secret_format",
            "dependency_advisory",
        }
        assert len(result.historical.dependency_result.packages) == 1
        assert result.historical.dependency_result.packages[0].assessment.status == "matched"
        assert canary not in result.model_dump_json().encode()
        assert b"private-content-canary" not in result.model_dump_json().encode()
        assert all(
            d.effective_disposition == "supported-static" for d in result.effective_decisions
        )

        class MissingBundle:
            def bundle(self, digest):
                raise ValueError("retained bundle unavailable")

        unavailable = await ScanReadService(api_factory, MissingBundle()).read(
            ctx, result.historical.spec.scope
        )
        dependency_ids = {f.id for f in result.historical.findings if f.entity.kind == "dependency"}
        assert len(dependency_ids) == 1
        assert all(
            d.effective_disposition == "unresolved" and d.reason == "bundle_unavailable"
            for d in unavailable.effective_decisions
            if d.finding_id in dependency_ids
        )
        assert unavailable.historical == result.historical
    finally:
        await capabilities.recover_owned()
        for value in owners:
            await value.cleanup_pending(controller)
    assert all(not list(value.root.glob("codespy-scan-*")) for value in owners)
