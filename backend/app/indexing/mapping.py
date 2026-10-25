"""Verified half-open safe-to-original envelopes with whole redaction intervals."""

import hashlib
import json
from array import array
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from app.contracts.indexing import IndexIntervalV1
from app.contracts.scanners import Digest
from app.domain.canonical_json import semantic_digest
from app.domain.errors import PersistenceError
from app.domain.scope import Contract
from app.services.privacy_transform import TransformOutput


class IndexMaterializationError(PersistenceError):
    pass


class MappedSpanV1(Contract):
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    safe_start_byte: int
    safe_end_byte: int
    content_sha256: Digest
    safe_content_sha256: Digest
    redaction_digest: Digest
    mappings: tuple[IndexIntervalV1, ...]


@dataclass(frozen=True, slots=True)
class VerifiedTransform:
    raw: bytes = field(repr=False)
    output: TransformOutput = field(repr=False)
    starts: tuple[int, ...]
    newlines: array[int] = field(repr=False)

    @classmethod
    def create(cls, raw: bytes, output: TransformOutput) -> "VerifiedTransform":
        try:
            if len(raw) > 2_097_152 or len(output.safe_bytes) > 16_777_216:
                raise ValueError("transform bound")
            raw.decode("utf-8")
            if output.safe_bytes.decode("utf-8") != output.safe_text:
                raise ValueError("safe text differs")
            safe_offset = original_offset = redactions = 0
            for item in output.mappings:
                if (
                    item.safe_start_byte != safe_offset
                    or item.original_start_byte != original_offset
                    or item.safe_end_byte <= safe_offset
                    or item.original_end_byte <= original_offset
                ):
                    raise ValueError("noncontiguous transform")
                safe = output.safe_bytes[safe_offset : item.safe_end_byte]
                original = raw[original_offset : item.original_end_byte]
                safe.decode("utf-8")
                original.decode("utf-8")
                if (
                    len(safe) != item.safe_end_byte - safe_offset
                    or len(original) != item.original_end_byte - original_offset
                ):
                    raise ValueError("transform exceeds input")
                if not item.is_redacted and safe != original:
                    raise ValueError("unredacted bytes differ")
                redactions += int(item.is_redacted)
                safe_offset, original_offset = item.safe_end_byte, item.original_end_byte
            if safe_offset != len(output.safe_bytes) or original_offset != len(raw):
                raise ValueError("incomplete transform")
            if output.redaction_count != redactions:
                raise ValueError("redaction count differs")
            payload = [item.model_dump() for item in output.mappings] if redactions else []
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            if hashlib.sha256(encoded).hexdigest() != output.redaction_map_digest:
                raise ValueError("transform digest differs")
        except (ValueError, UnicodeError):
            raise IndexMaterializationError("invalid_privacy_transform") from None
        return cls(
            raw,
            output,
            tuple(item.safe_start_byte for item in output.mappings),
            array("I", (i for i, value in enumerate(raw) if value == 10)),
        )

    def containing(self, offset: int) -> int:
        return bisect_right(self.starts, offset) - 1

    def envelope(self, start: int, end: int) -> MappedSpanV1:
        safe = self.output.safe_bytes
        if not 0 <= start < end <= len(safe):
            raise IndexMaterializationError("invalid_safe_range")
        for bound in (start, end):
            if bound < len(safe) and safe[bound] & 0xC0 == 0x80:
                raise IndexMaterializationError("invalid_safe_utf8_boundary")
        first = self.containing(start)
        last = self.containing(end)
        left = self.output.mappings[first]
        if left.is_redacted and left.safe_start_byte < start < left.safe_end_byte:
            start = left.safe_start_byte
        if last >= 0:
            right = self.output.mappings[last]
            if right.is_redacted and right.safe_start_byte < end < right.safe_end_byte:
                end = right.safe_end_byte
        clipped: list[tuple[int, int, int, int, bool]] = []
        for index in range(first, len(self.output.mappings)):
            item = self.output.mappings[index]
            if item.safe_start_byte >= end:
                break
            low, high = max(start, item.safe_start_byte), min(end, item.safe_end_byte)
            if item.is_redacted:
                original_low, original_high = item.original_start_byte, item.original_end_byte
            else:
                original_low = item.original_start_byte + low - item.safe_start_byte
                original_high = item.original_start_byte + high - item.safe_start_byte
            clipped.append((low, high, original_low, original_high, item.is_redacted))
        original_start, original_end = clipped[0][2], clipped[-1][3]
        mappings = tuple(
            IndexIntervalV1(
                safe_start_byte=a - start,
                safe_end_byte=b - start,
                original_start_byte=c - original_start,
                original_end_byte=d - original_start,
                is_redacted=e,
            )
            for a, b, c, d, e in clipped
        )
        return MappedSpanV1(
            start_byte=original_start,
            end_byte=original_end,
            start_line=bisect_left(self.newlines, original_start) + 1,
            end_line=bisect_left(self.newlines, original_end - 1) + 1,
            safe_start_byte=start,
            safe_end_byte=end,
            content_sha256=hashlib.sha256(self.raw[original_start:original_end]).hexdigest(),
            safe_content_sha256=hashlib.sha256(safe[start:end]).hexdigest(),
            redaction_digest=semantic_digest([item.model_dump() for item in mappings]),
            mappings=mappings,
        )


def safe_to_original_envelope(
    raw: bytes, output: TransformOutput, start: int, end: int
) -> MappedSpanV1:
    return VerifiedTransform.create(raw, output).envelope(start, end)
