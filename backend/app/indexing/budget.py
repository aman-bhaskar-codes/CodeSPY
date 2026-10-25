"""Incremental canonical byte accounting for bounded chunk publication."""

from dataclasses import dataclass, field

from app.contracts.indexing import MaterializedChunkV1, OmissionReason
from app.domain.canonical_json import canonical_bytes

# Covers scope, digests, 128 UUIDs and 128 examples with 1024-byte admitted paths.
HEADER_RESERVE = 262_144


@dataclass(slots=True)
class ChunkBudget:
    chunks: list[MaterializedChunkV1] = field(default_factory=list)
    safe_bytes: int = 0
    manifest_bytes: int = 0
    draft_bytes: int = 0

    def add(self, chunk: MaterializedChunkV1) -> OmissionReason | None:
        if len(self.chunks) >= 4096:
            return "chunk_limit"
        safe = len(chunk.safe_content.encode())
        if self.safe_bytes + safe > 16_777_216:
            return "safe_byte_limit"
        meta = len(canonical_bytes(chunk.model_dump(exclude={"safe_content"})))
        full = len(canonical_bytes(chunk.model_dump()))
        comma = int(bool(self.chunks))
        if self.manifest_bytes + meta + comma + HEADER_RESERVE > 8_388_608:
            return "manifest_limit"
        if self.draft_bytes + full + comma + HEADER_RESERVE > 25_165_824:
            return "draft_limit"
        self.chunks.append(chunk)
        self.safe_bytes += safe
        self.manifest_bytes += meta + comma
        self.draft_bytes += full + comma
        return None
