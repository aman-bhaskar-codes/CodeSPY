import hashlib
import os
from uuid import uuid4

from app.adapters.emulator_client import emulator_client
from app.adapters.storage.local import LocalObjectStore
from app.adapters.storage.s3 import S3ObjectStore
from app.models.phase2_tables import PHASE2_TABLES
from app.workers.cleanup import CleanupWorker
from sqlalchemy import func, insert

from tests.phase2.integration.test_submission_ingestion import submission_scenario


async def test_put_committed_before_version_ledger_update_is_deleted_exactly(factory, tmp_path):
    _, ctx, request, _, _, _ = await submission_scenario(factory, tmp_path)
    client = emulator_client("s3", "http://127.0.0.1:4567")
    bucket = "codespy-p2-test-" + os.environ["CODESPY_TEST_MARKER"]
    client.create_bucket(
        Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "ap-south-1"}
    )
    client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    try:
        store = await S3ObjectStore.qualified(client, bucket, LocalObjectStore(tmp_path / "spool"))
        key, body, identity = "source/orphan", b"actual-owned-orphan", uuid4()
        client.put_object(Bucket=bucket, Key=key, Body=body)
        async with factory.begin() as s:
            await s.execute(
                insert(PHASE2_TABLES["object_records"]).values(
                    org_id=ctx.org_id,
                    id=identity,
                    repository_id=request.repository_id,
                    key=key,
                    kind="source",
                    state="reserved",
                    sha256=hashlib.sha256(body).hexdigest(),
                    size_bytes=len(body),
                    expires_at=func.clock_timestamp(),
                )
            )
            await s.execute(
                insert(PHASE2_TABLES["cleanup_tasks"]).values(
                    org_id=ctx.org_id, id=uuid4(), object_id=identity, reason="orphan"
                )
            )
        assert await CleanupWorker(factory, store).tick() == 1
        assert await store.describe_owned(key, 104857600) is None
    finally:
        for page in client.get_paginator("list_object_versions").paginate(Bucket=bucket):
            versions = [
                {"Key": row["Key"], "VersionId": row["VersionId"]}
                for field in ["Versions", "DeleteMarkers"]
                for row in page.get(field, [])
            ]
            if versions:
                client.delete_objects(Bucket=bucket, Delete={"Objects": versions})
        client.delete_bucket(Bucket=bucket)
        client.close()
