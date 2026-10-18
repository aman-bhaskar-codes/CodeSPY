"""Storage boundary: immutable byte identities, never filesystem paths or URLs."""

from collections.abc import AsyncIterable, AsyncIterator
from typing import Protocol

from app.contracts.ingestion import ObjectDescriptor


class ObjectStore(Protocol):
    async def put_stage(
        self, key: str, chunks: AsyncIterable[bytes], max_bytes: int
    ) -> ObjectDescriptor: ...
    async def put_if_absent(self, key: str, source: ObjectDescriptor) -> ObjectDescriptor: ...
    def open_verified(self, descriptor: ObjectDescriptor) -> AsyncIterator[bytes]: ...
    async def delete_exact(self, descriptor: ObjectDescriptor) -> None: ...
