import os
from uuid import uuid4

import httpx
import pytest
from app.adapters.emulator_client import emulator_client
from app.adapters.queue.sqs import SQSQueue
from app.adapters.storage.local import LocalObjectStore
from app.adapters.storage.s3 import S3ObjectStore
from app.admission.process import DockerAdmissionRunner
from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.models.phase2_tables import PHASE2_TABLES
from app.services.job_finalization import JobFinalizer
from app.workers.consumer import JobConsumer
from app.workers.dispatcher import OutboxDispatcher
from app.workers.operations.admit_source import StoredAdmissionProcessor
from sqlalchemy import select
from tests.phase2.integration.test_submission_ingestion import submission_scenario


async def test_api_s3_sqs_linux_inventory_vertical_slice(factory, tmp_path):
    image = os.environ.get("CODESPY_ADMISSION_IMAGE")
    if not image:
        pytest.skip("owned pinned admission image required")
    _, ctx, reservation, _, body, _ = await submission_scenario(factory, tmp_path)
    marker = os.environ["CODESPY_TEST_MARKER"]
    s3, sqs = (emulator_client(name, "http://127.0.0.1:4567") for name in ["s3", "sqs"])
    bucket = "codespy-p2-test-" + marker
    s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    queue_url = sqs.create_queue(QueueName="codespy-p2-test-" + marker + "-" + uuid4().hex[:8])[
        "QueueUrl"
    ]
    try:
        store = await S3ObjectStore.qualified(s3, bucket, LocalObjectStore(tmp_path / "spool"))
        queue = SQSQueue(sqs, queue_url)
        app = create_app(
            ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
            ApplicationPorts(factory, store, ctx),
        )
        consumer = JobConsumer(
            factory,
            queue,
            JobFinalizer(factory, store),
            StoredAdmissionProcessor(
                factory, store, DockerAdmissionRunner(image, tmp_path / "runner")
            ),
        )
        dispatcher = OutboxDispatcher(factory, queue)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            session = (await client.get("/api/v1/session")).json()
            headers = {
                "Origin": "http://testserver",
                "X-CSRF-Token": session["csrf_token"],
                "Idempotency-Key": "vertical-upload",
            }
            response = await client.post(
                f"/api/v1/repositories/{reservation.repository_id}/uploads",
                json={
                    "repository_id": str(reservation.repository_id),
                    "filename": "source.zip",
                    "declared_size": len(body),
                },
                headers=headers,
            )
            assert response.status_code == 201, response.text
            sub = response.json()["id"]
            response = await client.put(
                f"/api/v1/submissions/{sub}/content",
                content=body,
                headers={**headers, "Content-Type": "application/zip"},
            )
            assert response.status_code == 202, response.text
            for _ in range(2):
                assert await dispatcher.dispatch_once() == 1
                deliveries = await queue.receive(1, wait_seconds=1)
                assert len(deliveries) == 1
                await consumer.handle(deliveries[0])
            final = (await client.get(f"/api/v1/submissions/{sub}")).json()
            assert final["state"] == "admitted"
            inventory = await client.get(f"/api/v1/snapshots/{final['snapshot_id']}/inventory")
            assert inventory.status_code == 200 and len(inventory.json()["items"]) == 1
            events = await client.get(
                "/api/v1/jobs/" + response.json()["job_id"] + "/events?limit=1"
            )
            assert events.status_code == 200 and events.json()["next_cursor"]
            cursor = events.json()["next_cursor"]
            assert (
                await client.get(
                    "/api/v1/jobs/" + response.json()["job_id"] + "/events",
                    params={"after": cursor},
                )
            ).status_code == 200
        async with factory() as s:
            states = (
                await s.scalars(
                    select(PHASE2_TABLES["jobs"].c.state).where(
                        PHASE2_TABLES["jobs"].c.org_id == ctx.org_id
                    )
                )
            ).all()
            assert states == ["succeeded", "succeeded"]
    finally:
        sqs.delete_queue(QueueUrl=queue_url)
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
            versions = [
                {"Key": row["Key"], "VersionId": row["VersionId"]}
                for key in ["Versions", "DeleteMarkers"]
                for row in page.get(key, [])
            ]
            if versions:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": versions})
        s3.delete_bucket(Bucket=bucket)
        s3.close()
        sqs.close()
