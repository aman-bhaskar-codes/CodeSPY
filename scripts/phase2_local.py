"""Run a disposable loopback CodeSPY fixture; no cloud or owner credentials.

Prepare phase2_env first. This command creates a separate owned database,
marker-named emulator resources and fixture identity. It drops only those
resources on exit. Never use this fixture entrypoint as a hosted deployment.
"""

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

import uvicorn
from app.adapters.emulator_client import emulator_client
from app.adapters.io import blocking_call
from app.adapters.queue.sqs import SQSQueue
from app.adapters.storage.local import LocalObjectStore
from app.adapters.storage.s3 import S3ObjectStore
from app.admission.process import DockerAdmissionRunner
from app.analyzers.runner import DockerAnalysisRunner
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.contracts.persistence import RepositoryCreate
from app.models.phase2_tables import PHASE2_TABLES
from app.repositories.identity_repository import IdentityRepository
from app.repositories.repository_repository import RepositoryRepository
from app.services.job_finalization import JobFinalizer
from app.services.structural_finalization import StructuralFinalizer
from app.workers.cleanup import CleanupWorker
from app.workers.consumer import JobConsumer
from app.workers.dispatcher import OutboxDispatcher
from app.workers.operations.admit_source import StoredAdmissionProcessor
from app.workers.operations.analyze_structure import StructuralAnalysisOperation
from app.workers.recovery import RecoveryWorker
from app.workers.runtime import WorkerRuntime
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts import phase2_db as harness


async def run(args: argparse.Namespace) -> None:
    values = harness.read_env(args.env_file)
    harness.verify_marker(values, values["CODESPY_TEST_DATABASE"])
    marker = uuid4().hex
    root = ROOT / "work" / "phase2" / ("runtime-" + marker)
    root.mkdir(mode=0o700, parents=True)
    bucket = "codespy-p2-test-" + marker
    ownership = root / "ownership.json"
    ownership.write_text(
        json.dumps(
            {
                "marker": marker,
                "bucket": bucket,
                "harness_marker": values["CODESPY_TEST_MARKER"],
            }
        )
    )
    ownership.chmod(0o600)
    database = harness.create_database(values, "local")
    engines, queue_urls = [], []
    s3 = sqs = None
    bucket_created = False
    try:
        harness.migrate(
            values,
            database,
            "0011_structural_authority"
            if args.analysis_image_file
            else "0009_api_commands",
        )
        harness.grant_app(values, database)

        def factory(role, field):
            engine = create_async_engine(
                harness.url(
                    values, database, role, values[field], driver="postgresql+asyncpg"
                ),
                pool_size=5,
                max_overflow=0,
                hide_parameters=True,
                connect_args={
                    "command_timeout": 20,
                    "server_settings": {"timezone": "UTC"},
                },
            )
            engines.append(engine)
            return async_sessionmaker(engine, expire_on_commit=False)

        bootstrap = factory("p2_migration", "CODESPY_TEST_MIGRATION_PASSWORD")
        async with bootstrap.begin() as session:
            ctx = await IdentityRepository(session).create_fixture(
                "local-" + marker[:12]
            )
            repo = await RepositoryRepository(session).create(
                ctx,
                RepositoryCreate(
                    repository_key="archive",
                    display_name="Local source archives",
                    source_kind="archive",
                ),
            )
            await session.execute(
                insert(PHASE2_TABLES["org_work_limits"]).values(org_id=ctx.org_id)
            )
            await session.execute(
                insert(PHASE2_TABLES["repository_grants"]).values(
                    org_id=ctx.org_id,
                    repository_id=repo.id,
                    principal_id=ctx.principal_id,
                    permission="write",
                )
            )
        api_factory = factory("p2_api", "CODESPY_TEST_APP_PASSWORD")
        broker = factory("p2_broker", "CODESPY_TEST_BROKER_PASSWORD")
        s3, sqs = (
            emulator_client(name, "http://127.0.0.1:4567") for name in ["s3", "sqs"]
        )
        s3.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
        )
        bucket_created = True
        s3.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        dead = sqs.create_queue(
            QueueName=bucket + "-dlq", Attributes={"MessageRetentionPeriod": "1209600"}
        )["QueueUrl"]
        queue_urls.append(dead)
        arn = sqs.get_queue_attributes(QueueUrl=dead, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]
        source = sqs.create_queue(
            QueueName=bucket + "-jobs",
            Attributes={
                "MessageRetentionPeriod": "345600",
                "VisibilityTimeout": "120",
                "RedrivePolicy": json.dumps(
                    {"deadLetterTargetArn": arn, "maxReceiveCount": "10"}
                ),
            },
        )["QueueUrl"]
        queue_urls.append(source)
        queue = SQSQueue(sqs, source)
        store = await S3ObjectStore.qualified(
            s3, bucket, LocalObjectStore(root / "objects")
        )
        runner = DockerAdmissionRunner(
            args.image_file.read_text().strip(), root / "admission"
        )
        await runner.qualify()

        analysis_runner = None
        toolchain = None
        if args.analysis_image_file is not None:
            analysis_runner = DockerAnalysisRunner(
                args.analysis_image_file.read_text().strip(), root / "analysis"
            )
            toolchain = await analysis_runner.readiness()

        async def ready():
            if (
                analysis_runner is not None
                and await analysis_runner.readiness() != toolchain
            ):
                return False
            async with api_factory() as session:
                await session.execute(text("SELECT 1"))
            await blocking_call(lambda: s3.head_bucket(Bucket=bucket))
            await blocking_call(
                lambda: sqs.get_queue_attributes(
                    QueueUrl=source, AttributeNames=["QueueArn"]
                )
            )
            return True

        app = create_app(
            ApiSettings(
                origin="http://127.0.0.1:5173",
                auth_mode="local_fixture",
                structural_enabled=analysis_runner is not None,
                structural_toolchain_hash=toolchain,
            ),
            ApplicationPorts(api_factory, store, ctx, readiness=ready),
        )
        if args.check:
            await ready()
            print(
                "Local API, database roles, versioned storage, queues and Linux admission are ready."
            )
            return
        consumer = JobConsumer(
            broker,
            queue,
            JobFinalizer(broker, store),
            StoredAdmissionProcessor(broker, store, runner),
            structural_operation=StructuralAnalysisOperation(
                broker,
                store,
                StructuralFinalizer(broker, store),
                analysis_runner,
            )
            if analysis_runner is not None
            else None,
        )
        runtime = WorkerRuntime(
            OutboxDispatcher(broker, queue, dead_letters=SQSQueue(sqs, dead)), consumer
        )
        stop = asyncio.Event()
        worker = asyncio.create_task(
            runtime.run(
                stop, RecoveryWorker(broker).tick, CleanupWorker(broker, store).tick
            )
        )
        try:
            print(
                "API: http://127.0.0.1:8000 · frontend: run npm --prefix frontend run dev"
            )
            await uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=8000,
                    access_log=False,
                    proxy_headers=False,
                )
            ).serve()
        finally:
            stop.set()
            await worker
    finally:
        for engine in engines:
            await engine.dispose()
        if sqs is not None:
            for url in reversed(queue_urls):
                sqs.delete_queue(QueueUrl=url)
            sqs.close()
        if s3 is not None:
            if bucket_created:
                for page in s3.get_paginator("list_object_versions").paginate(
                    Bucket=bucket
                ):
                    versions = [
                        {"Key": row["Key"], "VersionId": row["VersionId"]}
                        for key in ["Versions", "DeleteMarkers"]
                        for row in page.get(key, [])
                    ]
                    if versions:
                        s3.delete_objects(Bucket=bucket, Delete={"Objects": versions})
                s3.delete_bucket(Bucket=bucket)
            s3.close()
        harness.drop_database(values, database)
        # Preserve ownership/workspace on incomplete sandbox removal.
        if not list((root / "admission").glob("*/container.json")):
            shutil.rmtree(root)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--image-file", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--analysis-image-file", type=Path)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
