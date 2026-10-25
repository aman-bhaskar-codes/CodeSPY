"""Trusted admitted identity in, verified bounded bytes out; no caller filesystem paths."""

from typing import Protocol
from uuid import UUID

from app.contracts.ingestion import ObjectDescriptor
from app.domain.errors import PersistenceError
from app.domain.scope import Contract
from pydantic import Field


class SourceReaderUnavailable(PersistenceError):  # noqa: N818 - stable domain rejection name
    pass


class AdmittedSourceReadV1(Contract):
    org_id: UUID
    repository_id: UUID
    snapshot_id: UUID
    file_id: UUID
    archive: ObjectDescriptor
    path: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0, le=2_097_152)
    strip_root: bool = False


class SourceReaderPort(Protocol):
    async def read(self, request: AdmittedSourceReadV1) -> bytes: ...
