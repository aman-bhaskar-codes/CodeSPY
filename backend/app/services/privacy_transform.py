"""Privacy transformations, secret redaction, and byte-exact interval mapping."""

import fnmatch
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.contracts.source_view import (
    IntervalMappingV1,
    PrivacyPolicyV1,
)

# Compiled regex patterns for recognized secrets and canary tokens
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("canary", re.compile(r"codespy_sec_canary_[a-zA-Z0-9_\-]+")),
    (
        "aws_access_key",
        re.compile(r"(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}"),
    ),
    (
        "github_token",
        re.compile(r"(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{36,255}"),
    ),
    ("slack_token", re.compile(r"xox[baprs]-[0-9a-zA-Z]{10,48}")),
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9_-]+ )?PRIVATE KEY-----[^-]+"
            r"-----END (?:[A-Z0-9_-]+ )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    (
        "hardcoded_credential",
        re.compile(
            r"""(?i)(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*["']([^"'\r\n]{8,128})["']"""
        ),
    ),
)


@dataclass(frozen=True)
class SecretMatch:
    start_byte: int
    end_byte: int
    secret_type: str


@dataclass(frozen=True)
class TransformOutput:
    safe_bytes: bytes
    safe_text: str | None
    mappings: tuple[IntervalMappingV1, ...]
    redaction_map_digest: str
    redaction_count: int


def is_sensitive_path(path: str, patterns: Sequence[str]) -> bool:
    """Check if file path matches any sensitive path glob pattern."""
    normalized = PurePosixPath(path.strip("/"))
    path_str = str(normalized)
    name = normalized.name

    for pat in patterns:
        pat_clean = pat.strip("/")
        if fnmatch.fnmatch(name, pat_clean) or fnmatch.fnmatch(path_str, pat_clean):
            return True
        # Prefix matching for directory patterns like '.aws/*' or '.ssh/*'
        if pat_clean.endswith("/*"):
            dir_prefix = pat_clean[:-2]
            if path_str == dir_prefix or path_str.startswith(f"{dir_prefix}/"):
                return True
    return False


def _compute_char_to_byte_offsets(text: str) -> list[int]:
    """Compute mapping from character index to UTF-8 byte offset."""
    offsets = [0]
    total_bytes = 0
    for ch in text:
        total_bytes += len(ch.encode("utf-8", errors="surrogateescape"))
        offsets.append(total_bytes)
    return offsets


def _find_secret_byte_spans(text: str) -> list[SecretMatch]:
    """Identify secret byte ranges in UTF-8 text."""
    char_to_byte = _compute_char_to_byte_offsets(text)
    raw_spans: list[SecretMatch] = []

    for secret_type, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            # If pattern has capture group (e.g. hardcoded_credential value), redact group 1
            if match.lastindex and match.lastindex >= 1 and match.group(1):
                start_char, end_char = match.span(1)
            else:
                start_char, end_char = match.span()

            start_byte = char_to_byte[start_char]
            end_byte = char_to_byte[end_char]
            if start_byte < end_byte:
                raw_spans.append(
                    SecretMatch(
                        start_byte=start_byte,
                        end_byte=end_byte,
                        secret_type=secret_type,
                    )
                )

    # Sort matches by start_byte, then longer matches first
    raw_spans.sort(key=lambda s: (s.start_byte, -(s.end_byte - s.start_byte)))

    # Merge overlapping or contiguous spans
    merged: list[SecretMatch] = []
    for span in raw_spans:
        if not merged:
            merged.append(span)
            continue
        prev = merged[-1]
        if span.start_byte < prev.end_byte:
            # Overlapping span: merge and keep existing type or combine
            merged[-1] = SecretMatch(
                start_byte=prev.start_byte,
                end_byte=max(prev.end_byte, span.end_byte),
                secret_type=prev.secret_type,
            )
        elif span.start_byte == prev.end_byte and span.secret_type == prev.secret_type:
            # Adjacent identical type: merge
            merged[-1] = SecretMatch(
                start_byte=prev.start_byte,
                end_byte=span.end_byte,
                secret_type=prev.secret_type,
            )
        else:
            merged.append(span)

    return merged


def transform_model_safe(
    raw_bytes: bytes,
    policy: PrivacyPolicyV1,
) -> TransformOutput:
    """Redact recognized secrets and produce byte-exact interval mappings."""
    if not policy.redact_secrets or not raw_bytes:
        # Identity mapping for empty or unredacted content
        digest = hashlib.sha256(b"[]").hexdigest()
        mapping = (
            (
                IntervalMappingV1(
                    safe_start_byte=0,
                    safe_end_byte=len(raw_bytes),
                    original_start_byte=0,
                    original_end_byte=len(raw_bytes),
                    is_redacted=False,
                ),
            )
            if raw_bytes
            else ()
        )
        text = raw_bytes.decode("utf-8", errors="replace") if raw_bytes else ""
        return TransformOutput(
            safe_bytes=raw_bytes,
            safe_text=text,
            mappings=mapping,
            redaction_map_digest=digest,
            redaction_count=0,
        )

    # Decode text for secret scanning
    try:
        text = raw_bytes.decode("utf-8")
        is_valid_utf8 = True
    except UnicodeDecodeError:
        # Preserve each undecodable byte while computing offsets into the original.
        # Replacement decoding would change byte widths before recognized secrets.
        text = raw_bytes.decode("utf-8", errors="surrogateescape")
        is_valid_utf8 = False

    spans = _find_secret_byte_spans(text)
    if not spans:
        digest = hashlib.sha256(b"[]").hexdigest()
        mappings = (
            IntervalMappingV1(
                safe_start_byte=0,
                safe_end_byte=len(raw_bytes),
                original_start_byte=0,
                original_end_byte=len(raw_bytes),
                is_redacted=False,
            ),
        )
        safe_text = text if is_valid_utf8 else raw_bytes.decode("utf-8", errors="replace")
        return TransformOutput(
            safe_bytes=raw_bytes,
            safe_text=safe_text,
            mappings=mappings,
            redaction_map_digest=digest,
            redaction_count=0,
        )

    safe_buf = bytearray()
    mappings_list: list[IntervalMappingV1] = []
    prev_orig_end = 0

    for span in spans:
        # 1. Unredacted segment preceding secret
        if span.start_byte > prev_orig_end:
            unredacted_chunk = raw_bytes[prev_orig_end : span.start_byte]
            safe_start = len(safe_buf)
            safe_buf.extend(unredacted_chunk)
            safe_end = len(safe_buf)
            mappings_list.append(
                IntervalMappingV1(
                    safe_start_byte=safe_start,
                    safe_end_byte=safe_end,
                    original_start_byte=prev_orig_end,
                    original_end_byte=span.start_byte,
                    is_redacted=False,
                )
            )

        # 2. Redacted secret replacement
        replacement_text = policy.replacement_template.format(secret_type=span.secret_type)
        replacement_bytes = replacement_text.encode("utf-8")
        safe_start = len(safe_buf)
        safe_buf.extend(replacement_bytes)
        safe_end = len(safe_buf)
        mappings_list.append(
            IntervalMappingV1(
                safe_start_byte=safe_start,
                safe_end_byte=safe_end,
                original_start_byte=span.start_byte,
                original_end_byte=span.end_byte,
                is_redacted=True,
                redaction_type=span.secret_type,
            )
        )
        prev_orig_end = span.end_byte

    # 3. Trailing unredacted segment
    if prev_orig_end < len(raw_bytes):
        unredacted_chunk = raw_bytes[prev_orig_end:]
        safe_start = len(safe_buf)
        safe_buf.extend(unredacted_chunk)
        safe_end = len(safe_buf)
        mappings_list.append(
            IntervalMappingV1(
                safe_start_byte=safe_start,
                safe_end_byte=safe_end,
                original_start_byte=prev_orig_end,
                original_end_byte=len(raw_bytes),
                is_redacted=False,
            )
        )

    safe_bytes_result = bytes(safe_buf)
    safe_text_result = (
        safe_bytes_result.decode("utf-8")
        if is_valid_utf8
        else safe_bytes_result.decode("utf-8", errors="replace")
    )

    # Compute deterministic digest over serialized mappings
    serialized_map = json.dumps(
        [m.model_dump() for m in mappings_list],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    map_digest = hashlib.sha256(serialized_map).hexdigest()

    return TransformOutput(
        safe_bytes=safe_bytes_result,
        safe_text=safe_text_result,
        mappings=tuple(mappings_list),
        redaction_map_digest=map_digest,
        redaction_count=len(spans),
    )


def map_safe_offset_to_original(
    mappings: Sequence[IntervalMappingV1],
    safe_offset: int,
) -> int:
    """Map a byte offset in safe transformed text back to the original byte offset."""
    if not mappings:
        return safe_offset

    for m in mappings:
        if m.safe_start_byte <= safe_offset <= m.safe_end_byte:
            if m.is_redacted:
                # Inside redacted interval: anchor to start of original secret span
                return m.original_start_byte
            offset_inside = safe_offset - m.safe_start_byte
            return m.original_start_byte + offset_inside

    # Beyond mapped range: anchor to last interval endpoint
    last = mappings[-1]
    if safe_offset >= last.safe_end_byte:
        return last.original_end_byte
    return 0


def map_original_offset_to_safe(
    mappings: Sequence[IntervalMappingV1],
    orig_offset: int,
) -> int:
    """Map an original byte offset to the corresponding safe transformed byte offset."""
    if not mappings:
        return orig_offset

    for m in mappings:
        if m.original_start_byte <= orig_offset <= m.original_end_byte:
            if m.is_redacted:
                return m.safe_start_byte
            offset_inside = orig_offset - m.original_start_byte
            return m.safe_start_byte + offset_inside

    last = mappings[-1]
    if orig_offset >= last.original_end_byte:
        return last.safe_end_byte
    return 0


def slice_mappings_for_safe_range(
    mappings: Sequence[IntervalMappingV1],
    safe_start: int,
    safe_end: int,
) -> tuple[IntervalMappingV1, ...]:
    """Extract and re-index interval mappings covering a sliced safe byte range."""
    sliced: list[IntervalMappingV1] = []
    for m in mappings:
        # Check if interval overlaps with [safe_start, safe_end)
        overlap_start = max(safe_start, m.safe_start_byte)
        overlap_end = min(safe_end, m.safe_end_byte)
        if overlap_start < overlap_end or (
            safe_start == safe_end and m.safe_start_byte <= safe_start <= m.safe_end_byte
        ):
            sliced.append(m)
    return tuple(sliced)
