"""Trusted offline index preparation; local paths never cross the public API."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from app.contracts.indexing import TOKENIZER_SHA256, IndexFileV1, IndexSpecV1
from app.contracts.scanners import Digest
from app.domain.errors import PersistenceError
from app.domain.scope import Contract

if TYPE_CHECKING:
    from app.indexing.receipt import PreparedIndexReceipt


class IndexBuilderUnavailable(PersistenceError):  # noqa: N818 - stable unavailable boundary
    def __init__(self, message: str, *, failure_code: str = "index_runtime_unavailable"):
        super().__init__(message)
        if failure_code not in {
            "index_runtime_unavailable",
            "index_timeout",
            "index_process_failed",
            "index_cleanup_pending",
            "integrity_failure",
        }:
            raise ValueError("invalid index failure classification")
        self.failure_code = failure_code


class IndexCapabilitiesV1(Contract):
    runtime_source_sha256: Digest
    tokenizer_sha256: Literal[
        "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
    ] = TOKENIZER_SHA256
    max_request_bytes: Literal[98368]
    max_files: Literal[128] = 128
    max_source_bytes: Literal[16777216] = 16_777_216
    max_output_bytes: Literal[25165824] = 25_165_824
    max_seconds: Literal[180] = 180


@dataclass(frozen=True, slots=True)
class VerifiedIndexInputV1:
    file: IndexFileV1
    owned_path: Path = field(repr=False)


class IndexBuilderPort(Protocol):
    async def capabilities(self) -> IndexCapabilitiesV1: ...
    async def build(
        self, spec: IndexSpecV1, inputs: tuple[VerifiedIndexInputV1, ...], *, timeout_seconds: float
    ) -> "PreparedIndexReceipt": ...
    async def recover_owned(self) -> None: ...
