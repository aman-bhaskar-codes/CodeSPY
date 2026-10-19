"""Versioned, conditional object storage for the explicitly owned loopback emulator."""

import base64
import hashlib
import os
import re
from collections.abc import AsyncIterable, AsyncIterator
from functools import partial
from typing import Any
from uuid import uuid4

from app.adapters.io import blocking_call
from app.adapters.storage.local import LocalObjectStore
from app.contracts.ingestion import ObjectDescriptor
from app.domain.errors import DuplicateConflict, ProviderCapacityUnsupported
from botocore.exceptions import ClientError


class S3ObjectStore:
    def __init__(self, client: Any, bucket: str, spool: LocalObjectStore):
        if client.meta.endpoint_url != "http://127.0.0.1:4567" or not re.fullmatch(
            r"codespy-p2-test-[a-f0-9]{32}", bucket
        ):
            raise ValueError("owned emulator bucket required")
        self.client = client
        self.bucket = bucket
        self.spool = spool

    @classmethod
    async def qualified(cls, client: Any, bucket: str, spool: LocalObjectStore) -> "S3ObjectStore":
        store = cls(client, bucket, spool)
        if (await blocking_call(lambda: client.get_bucket_versioning(Bucket=bucket))).get(
            "Status"
        ) != "Enabled":
            raise ProviderCapacityUnsupported("object versioning unavailable")
        key = "qualification/" + uuid4().hex
        versions = []
        try:
            first = await blocking_call(
                lambda: client.put_object(Bucket=bucket, Key=key, Body=b"a", IfNoneMatch="*")
            )
            version = first.get("VersionId")
            if version is not None:
                versions.append(version)
            if not version or version == "null":
                raise ProviderCapacityUnsupported("version identity unavailable")
            try:
                second = await blocking_call(
                    lambda: client.put_object(Bucket=bucket, Key=key, Body=b"b", IfNoneMatch="*")
                )
            except ClientError as error:
                # S3 does not model these conditional HTTP failures as dedicated exceptions.
                if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") not in {
                    409,
                    412,
                }:
                    raise
            else:
                if second.get("VersionId"):
                    versions.append(second["VersionId"])
                raise ProviderCapacityUnsupported("conditional creation unavailable")
            head = await blocking_call(lambda: client.head_object(Bucket=bucket, Key=key))
            if head.get("VersionId") != version:
                raise ProviderCapacityUnsupported("conditional creation changed stored version")
        finally:
            for version in versions:
                await blocking_call(
                    partial(client.delete_object, Bucket=bucket, Key=key, VersionId=version)
                )
        return store

    async def put_stage(
        self, key: str, chunks: AsyncIterable[bytes], max_bytes: int
    ) -> ObjectDescriptor:
        return await self.spool.put_stage(key, chunks, max_bytes)

    async def put_if_absent(self, key: str, source: ObjectDescriptor) -> ObjectDescriptor:
        if key.startswith("stage/"):
            raise ValueError("canonical key required")
        ObjectDescriptor(key=key, sha256=source.sha256, size_bytes=source.size_bytes)
        temporary = None
        if source.version_id is not None:
            temporary = await self.spool.put_stage(
                "stage/copy/" + uuid4().hex, self.open_verified(source), source.size_bytes or 1
            )
            local = temporary
        else:
            local = source

        def put() -> dict[str, Any]:
            self.spool._verify(local, collect=False)
            parent, name = self.spool._parent(local.key)
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                with os.fdopen(fd, "rb") as body:
                    return dict(
                        self.client.put_object(
                            Bucket=self.bucket,
                            Key=key,
                            Body=body,
                            IfNoneMatch="*",
                            ChecksumSHA256=base64.b64encode(bytes.fromhex(source.sha256)).decode(),
                        )
                    )
            finally:
                os.close(parent)

        try:
            try:
                response = await blocking_call(put)
            except ClientError as error:
                if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") not in {
                    409,
                    412,
                }:
                    raise
                head = await blocking_call(
                    lambda: self.client.head_object(Bucket=self.bucket, Key=key)
                )
                if head["ContentLength"] != source.size_bytes:
                    raise DuplicateConflict("canonical content differs") from None
                response = head
            version = response.get("VersionId")
            if not version or version == "null":
                raise ProviderCapacityUnsupported("version identity unavailable")
            result = ObjectDescriptor(
                key=key, sha256=source.sha256, size_bytes=source.size_bytes, version_id=version
            )
            async for _ in self.open_verified(result):
                pass
            return result
        finally:
            if temporary is not None:
                await self.spool.delete_exact(temporary)

    async def open_verified(self, obj: ObjectDescriptor) -> AsyncIterator[bytes]:
        if obj.version_id is None:
            if not obj.key.startswith("stage/"):
                raise ValueError("S3 object version required")
            async for block in self.spool.open_verified(obj):
                yield block
            return
        response = await blocking_call(
            lambda: self.client.get_object(
                Bucket=self.bucket, Key=obj.key, VersionId=obj.version_id
            )
        )
        body = response["Body"]
        digest = hashlib.sha256()
        size = 0
        try:
            if response.get("ContentLength") != obj.size_bytes:
                raise DuplicateConflict("object length differs")
            while block := await blocking_call(lambda: body.read(65536)):
                size += len(block)
                if size > obj.size_bytes:
                    raise DuplicateConflict("object exceeds descriptor")
                digest.update(block)
                yield block
            if size != obj.size_bytes or digest.hexdigest() != obj.sha256:
                raise DuplicateConflict("object digest differs")
        finally:
            await blocking_call(body.close)

    async def read_verified(self, obj: ObjectDescriptor) -> bytes:
        result = bytearray()
        async for block in self.open_verified(obj):
            result.extend(block)
        return bytes(result)

    async def delete_exact(self, obj: ObjectDescriptor) -> None:
        if obj.version_id is None:
            await self.spool.delete_exact(obj)
            return
        try:
            async for _ in self.open_verified(obj):
                pass
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {
                "NoSuchKey",
                "NoSuchVersion",
                "404",
            }:
                raise
            return
        await blocking_call(
            lambda: self.client.delete_object(
                Bucket=self.bucket, Key=obj.key, VersionId=obj.version_id
            )
        )

    async def describe_owned(self, key: str, max_bytes: int) -> ObjectDescriptor | None:
        ObjectDescriptor(key=key, sha256="0" * 64, size_bytes=0)
        if not 0 < max_bytes <= 104857600:
            raise ValueError("invalid recovery bound")
        if key.startswith("stage/"):
            return await self.spool.describe_owned(key, max_bytes)
        try:
            head = await blocking_call(lambda: self.client.head_object(Bucket=self.bucket, Key=key))
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise
        version = head.get("VersionId")
        if not version or version == "null" or head["ContentLength"] > max_bytes:
            raise ValueError("orphan descriptor unavailable")
        result = await blocking_call(
            lambda: self.client.get_object(Bucket=self.bucket, Key=key, VersionId=version)
        )
        body = result["Body"]
        digest, size = hashlib.sha256(), 0
        try:
            while block := await blocking_call(lambda: body.read(65536)):
                size += len(block)
                if size > max_bytes:
                    raise ValueError("orphan exceeds recovery bound")
                digest.update(block)
        finally:
            await blocking_call(body.close)
        if size != head["ContentLength"]:
            raise DuplicateConflict("orphan descriptor differs")
        return ObjectDescriptor(
            key=key, sha256=digest.hexdigest(), size_bytes=size, version_id=version
        )
