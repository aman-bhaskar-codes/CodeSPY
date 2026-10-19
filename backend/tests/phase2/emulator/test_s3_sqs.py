import asyncio
import os
from uuid import uuid4

import pytest
from app.adapters.emulator_client import emulator_client
from app.adapters.queue.sqs import SQSQueue
from app.adapters.storage.local import LocalObjectStore
from app.adapters.storage.s3 import S3ObjectStore
from app.domain.errors import DuplicateConflict


async def chunks(value):
    yield value


async def test_emulator_immutable_versioned_storage(tmp_path):
    marker = os.environ["CODESPY_TEST_MARKER"]
    bucket = "codespy-p2-test-" + marker
    client = emulator_client("s3", "http://127.0.0.1:4567")
    client.create_bucket(
        Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "ap-south-1"}
    )
    client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    store = await S3ObjectStore.qualified(client, bucket, LocalObjectStore(tmp_path / "spool"))
    try:
        stage = await store.put_stage("stage/a", chunks(b"abc"), 3)
        a, b = await asyncio.gather(
            store.put_if_absent("source/a", stage), store.put_if_absent("source/a", stage)
        )
        assert a == b and a.version_id is not None
        assert await store.read_verified(a) == b"abc"
        wrong = await store.put_stage("stage/b", chunks(b"different"), 20)
        with pytest.raises(DuplicateConflict):
            await store.put_if_absent("source/a", wrong)
        await store.delete_exact(a)
        await store.delete_exact(a)
    finally:
        # Owned fixture bucket only; versions are enumerated through the SDK paginator.
        for page in client.get_paginator("list_object_versions").paginate(Bucket=bucket):
            items = [
                {"Key": x["Key"], "VersionId": x["VersionId"]}
                for field in ["Versions", "DeleteMarkers"]
                for x in page.get(field, [])
            ]
            if items:
                client.delete_objects(Bucket=bucket, Delete={"Objects": items})
        client.delete_bucket(Bucket=bucket)
        client.close()


async def test_emulator_sqs_delivery_and_visibility():
    client = emulator_client("sqs", "http://127.0.0.1:4567")
    name = "codespy-p2-test-" + os.environ["CODESPY_TEST_MARKER"] + "-" + uuid4().hex[:8]
    url = client.create_queue(QueueName=name)["QueueUrl"]
    queue = SQSQueue(client, url)
    try:
        await queue.send(b'{"fixture":true}')
        received = await queue.receive(1, wait_seconds=0)
        assert len(received) == 1
        await queue.extend(received[0].receipt, 0)
        again = await queue.receive(1, wait_seconds=0)
        assert again and again[0].body == received[0].body
        await queue.ack(again[0].receipt)
        assert not await queue.receive(1, wait_seconds=0)
    finally:
        client.delete_queue(QueueUrl=url)
        client.close()
