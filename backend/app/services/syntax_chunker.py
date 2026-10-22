"""Syntax-aware code chunking utilizing structural CPG observations and bounded windowing."""

import hashlib
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid5

import tiktoken

from app.contracts.chunking import ChunkKind, ChunkProfileV1, SourceChunkV1
from app.contracts.source_view import PrivacyPolicyV1
from app.services.privacy_transform import transform_model_safe

CHUNK_NAMESPACE = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

_ENCODER_CACHE: dict[str, tiktoken.Encoding] = {}


def get_tokenizer(name: str = "cl100k_base") -> tiktoken.Encoding:
    if name not in _ENCODER_CACHE:
        try:
            _ENCODER_CACHE[name] = tiktoken.get_encoding(name)
        except Exception:
            _ENCODER_CACHE[name] = tiktoken.get_encoding("cl100k_base")
    return _ENCODER_CACHE[name]


def count_tokens(text: str, tokenizer_name: str = "cl100k_base") -> int:
    enc = get_tokenizer(tokenizer_name)
    return len(enc.encode(text, disallowed_special=()))


def _compute_line_starts(content: bytes) -> list[int]:
    """Compute 0-indexed byte offsets where each line begins."""
    starts = [0]
    for idx, b in enumerate(content):
        if b == 10:  # ord('\n')
            starts.append(idx + 1)
    return starts


def _byte_to_line_number(line_starts: list[int], byte_offset: int) -> int:
    """Binary search / scan line number (1-indexed) for a given byte offset."""
    # Binary search for position in line_starts
    low = 0
    high = len(line_starts) - 1
    while low <= high:
        mid = (low + high) // 2
        if line_starts[mid] <= byte_offset:
            low = mid + 1
        else:
            high = mid - 1
    return max(1, high + 1)


class SyntaxAwareChunker:
    """Produces deterministic, syntax-preserving chunks from source code and CPG nodes."""

    def __init__(
        self,
        profile: ChunkProfileV1 | None = None,
        privacy_policy: PrivacyPolicyV1 | None = None,
    ) -> None:
        self.profile = profile or ChunkProfileV1()
        self.policy = privacy_policy or PrivacyPolicyV1()
        self.enc = get_tokenizer(self.profile.tokenizer_name)

    def chunk_file(
        self,
        org_id: UUID,
        repository_id: UUID,
        snapshot_id: UUID,
        file_id: UUID,
        file_path: str,
        raw_content: bytes,
        cpg_nodes: Sequence[dict[str, Any]] | None = None,
    ) -> list[SourceChunkV1]:
        """Chunk a single file into syntax units or bounded range windows."""
        if not raw_content:
            return []

        line_starts = _compute_line_starts(raw_content)
        total_len = len(raw_content)

        # Case 1: Structural CPG nodes available
        if cpg_nodes:
            chunks = self._chunk_from_cpg_nodes(
                org_id=org_id,
                repository_id=repository_id,
                snapshot_id=snapshot_id,
                file_id=file_id,
                file_path=file_path,
                raw_content=raw_content,
                cpg_nodes=cpg_nodes,
                line_starts=line_starts,
            )
            if chunks:
                return chunks

        # Case 2: File is small enough to fit within target bound entirely
        estimated_file_tokens = count_tokens(
            raw_content.decode("utf-8", errors="replace"), self.profile.tokenizer_name
        )
        if (
            estimated_file_tokens <= self.profile.target_tokens
            and total_len <= self.profile.max_bytes
        ):
            return [
                self._build_chunk(
                    org_id=org_id,
                    repository_id=repository_id,
                    snapshot_id=snapshot_id,
                    file_id=file_id,
                    file_path=file_path,
                    chunk_index=0,
                    chunk_kind=ChunkKind.FILE,
                    symbol_name=None,
                    qualified_name=None,
                    cpg_node_id=None,
                    start_byte=0,
                    end_byte=total_len,
                    line_starts=line_starts,
                    raw_content=raw_content,
                )
            ]

        # Case 3: Line-based windowing fallback (for non-CPG files or unparsed content)
        return self._chunk_windowed(
            org_id=org_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            file_id=file_id,
            file_path=file_path,
            raw_content=raw_content,
            line_starts=line_starts,
            start_byte=0,
            end_byte=total_len,
            base_kind=ChunkKind.WINDOW,
            symbol_name=None,
            qualified_name=None,
            cpg_node_id=None,
            start_index=0,
        )

    def _chunk_from_cpg_nodes(
        self,
        org_id: UUID,
        repository_id: UUID,
        snapshot_id: UUID,
        file_id: UUID,
        file_path: str,
        raw_content: bytes,
        cpg_nodes: Sequence[dict[str, Any]],
        line_starts: list[int],
    ) -> list[SourceChunkV1]:
        # Filter relevant structural definitions
        target_kinds = {"class", "function", "method", "module"}
        valid_nodes = [
            n
            for n in cpg_nodes
            if n.get("node_kind") in target_kinds
            and n.get("start_byte", 0) < n.get("end_byte", 0)
            and n.get("end_byte", 0) <= len(raw_content)
        ]
        if not valid_nodes:
            return []

        # Sort nodes by start_byte, then broader range first
        valid_nodes.sort(
            key=lambda n: (
                int(n["start_byte"]),
                -(int(n["end_byte"]) - int(n["start_byte"])),
            )
        )

        chunks: list[SourceChunkV1] = []
        covered_spans: list[tuple[int, int]] = []
        chunk_idx = 0

        for n in valid_nodes:
            sb = int(n["start_byte"])
            eb = int(n["end_byte"])
            unit_bytes = raw_content[sb:eb]
            if not unit_bytes:
                continue

            node_id = UUID(str(n["local_id"])) if "local_id" in n else None
            node_kind = ChunkKind(str(n["node_kind"]))
            sym_name = str(n.get("symbol_name") or "") or None
            qual_name = str(n.get("qualified_name") or "") or None

            unit_text = unit_bytes.decode("utf-8", errors="replace")
            token_count = count_tokens(unit_text, self.profile.tokenizer_name)

            if token_count <= self.profile.max_tokens and len(unit_bytes) <= self.profile.max_bytes:
                # Syntax unit fits in single chunk
                chunks.append(
                    self._build_chunk(
                        org_id=org_id,
                        repository_id=repository_id,
                        snapshot_id=snapshot_id,
                        file_id=file_id,
                        file_path=file_path,
                        chunk_index=chunk_idx,
                        chunk_kind=node_kind,
                        symbol_name=sym_name,
                        qualified_name=qual_name,
                        cpg_node_id=node_id,
                        start_byte=sb,
                        end_byte=eb,
                        line_starts=line_starts,
                        raw_content=raw_content,
                    )
                )
                chunk_idx += 1
                covered_spans.append((sb, eb))
            else:
                # Oversized syntax unit: split into contiguous windows
                sub_chunks = self._chunk_windowed(
                    org_id=org_id,
                    repository_id=repository_id,
                    snapshot_id=snapshot_id,
                    file_id=file_id,
                    file_path=file_path,
                    raw_content=raw_content,
                    line_starts=line_starts,
                    start_byte=sb,
                    end_byte=eb,
                    base_kind=ChunkKind.WINDOW,
                    symbol_name=sym_name,
                    qualified_name=qual_name,
                    cpg_node_id=node_id,
                    start_index=chunk_idx,
                )
                chunks.extend(sub_chunks)
                chunk_idx += len(sub_chunks)
                covered_spans.append((sb, eb))

        # Check for uncovered prefix (e.g. module header, imports) before first node
        if covered_spans:
            first_covered_start = min(s[0] for s in covered_spans)
            if first_covered_start > 64:
                prefix_chunk = self._build_chunk(
                    org_id=org_id,
                    repository_id=repository_id,
                    snapshot_id=snapshot_id,
                    file_id=file_id,
                    file_path=file_path,
                    chunk_index=chunk_idx,
                    chunk_kind=ChunkKind.MODULE,
                    symbol_name="header",
                    qualified_name=f"{file_path}:header",
                    cpg_node_id=None,
                    start_byte=0,
                    end_byte=first_covered_start,
                    line_starts=line_starts,
                    raw_content=raw_content,
                )
                chunks.append(prefix_chunk)

        # Sort chunks deterministically by start_byte and re-index chunk_index
        chunks.sort(key=lambda c: (c.start_byte, c.end_byte))
        reindexed = [c.model_copy(update={"chunk_index": i}) for i, c in enumerate(chunks)]
        return reindexed

    def _chunk_windowed(
        self,
        org_id: UUID,
        repository_id: UUID,
        snapshot_id: UUID,
        file_id: UUID,
        file_path: str,
        raw_content: bytes,
        line_starts: list[int],
        start_byte: int,
        end_byte: int,
        base_kind: ChunkKind,
        symbol_name: str | None,
        qualified_name: str | None,
        cpg_node_id: UUID | None,
        start_index: int,
    ) -> list[SourceChunkV1]:
        """Split a byte interval into contiguous, newline-aligned windows."""
        chunks: list[SourceChunkV1] = []
        curr_start = start_byte
        idx = start_index

        # Approximate byte target based on 3.8 bytes per token average
        target_byte_size = min(self.profile.max_bytes, self.profile.target_tokens * 4)
        overlap_bytes = self.profile.overlap_tokens * 4

        while curr_start < end_byte:
            candidate_end = min(end_byte, curr_start + target_byte_size)

            # Align candidate_end to next newline if within bounds
            if candidate_end < end_byte:
                next_nl = raw_content.find(b"\n", candidate_end - 128, candidate_end + 256)
                if next_nl != -1 and next_nl < end_byte:
                    candidate_end = next_nl + 1

            chunk_bytes = raw_content[curr_start:candidate_end]
            if not chunk_bytes:
                break

            chunks.append(
                self._build_chunk(
                    org_id=org_id,
                    repository_id=repository_id,
                    snapshot_id=snapshot_id,
                    file_id=file_id,
                    file_path=file_path,
                    chunk_index=idx,
                    chunk_kind=base_kind,
                    symbol_name=symbol_name,
                    qualified_name=qualified_name,
                    cpg_node_id=cpg_node_id,
                    start_byte=curr_start,
                    end_byte=candidate_end,
                    line_starts=line_starts,
                    raw_content=raw_content,
                )
            )
            idx += 1

            if candidate_end >= end_byte:
                break

            # Advance by step, ensuring progress
            step = max(128, (candidate_end - curr_start) - overlap_bytes)
            curr_start += step
            # Align curr_start to line start if possible
            next_line_start = raw_content.find(b"\n", curr_start - 64, curr_start + 64)
            if next_line_start != -1 and next_line_start + 1 < candidate_end:
                curr_start = next_line_start + 1

        return chunks

    def _build_chunk(
        self,
        org_id: UUID,
        repository_id: UUID,
        snapshot_id: UUID,
        file_id: UUID,
        file_path: str,
        chunk_index: int,
        chunk_kind: ChunkKind,
        symbol_name: str | None,
        qualified_name: str | None,
        cpg_node_id: UUID | None,
        start_byte: int,
        end_byte: int,
        line_starts: list[int],
        raw_content: bytes,
    ) -> SourceChunkV1:
        chunk_bytes = raw_content[start_byte:end_byte]
        sha = hashlib.sha256(chunk_bytes).hexdigest()

        # Apply privacy transform to chunk content
        safe_out = transform_model_safe(chunk_bytes, self.policy)
        token_count = count_tokens(safe_out.safe_text or "", self.profile.tokenizer_name)

        start_line = _byte_to_line_number(line_starts, start_byte)
        end_line = _byte_to_line_number(line_starts, max(start_byte, end_byte - 1))

        stable_key = (
            f"{snapshot_id}:{file_id}:{chunk_index}:{start_byte}:{end_byte}:"
            f"{self.profile.chunker_version}"
        )
        chunk_id = uuid5(CHUNK_NAMESPACE, stable_key)

        return SourceChunkV1(
            chunk_id=chunk_id,
            org_id=org_id,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            file_id=file_id,
            file_path=file_path,
            chunk_index=chunk_index,
            chunk_kind=chunk_kind,
            symbol_name=symbol_name,
            qualified_name=qualified_name,
            cpg_node_id=cpg_node_id,
            start_byte=start_byte,
            end_byte=end_byte,
            start_line=start_line,
            end_line=end_line,
            content_sha256=sha,
            byte_count=len(chunk_bytes),
            token_count=token_count,
            raw_content=chunk_bytes,
            safe_content=safe_out.safe_text or "",
            redaction_digest=safe_out.redaction_map_digest,
            chunker_version=self.profile.chunker_version,
        )
