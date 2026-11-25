"""Shared handling of text on its way to a model: truncation, redaction and evidence ids."""

import hashlib
import re
from dataclasses import dataclass

from app.contracts.source_view import DEFAULT_SENSITIVE_PATH_PATTERNS, PrivacyPolicyV1
from app.services.privacy_transform import SECRET_PATTERNS, is_sensitive_path

_POLICY = PrivacyPolicyV1()


@dataclass(frozen=True)
class Truncated:
    text: str
    truncated: bool
    bytes_total: int


def truncate_head_tail(text: str, limit: int) -> Truncated:
    """At most ``limit`` bytes, keeping the start and the end and saying how much was cut.

    Tool output is read from both ends: a command's first lines show what ran and its last
    lines show how it ended, so dropping the middle loses the least.
    """
    raw = text.encode()
    total = len(raw)
    if total <= limit:
        return Truncated(text, False, total)
    marker = f"\n[... {total - limit + 80} bytes omitted ...]\n"
    room = limit - len(marker.encode())
    head, tail = (room * 3) // 5, room - (room * 3) // 5
    head_text = raw[:head].decode("utf-8", errors="ignore")
    tail_text = raw[total - tail :].decode("utf-8", errors="ignore") if tail > 0 else ""
    omitted = total - len(head_text.encode()) - len(tail_text.encode())
    marker = f"\n[... {omitted} bytes omitted ...]\n"
    return Truncated(head_text + marker + tail_text, True, total)


def redact(text: str) -> tuple[str, int]:
    """Replace recognised secrets with markers, keeping every newline so line numbers stay true."""
    spans: list[tuple[int, int, str]] = []
    for secret_type, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span(1) if match.lastindex and match.group(1) else match.span()
            if start < end:
                spans.append((start, end, secret_type))
    if not spans:
        return text, 0
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    merged: list[tuple[int, int, str]] = []
    for start, end, kind in spans:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end), merged[-1][2])
        else:
            merged.append((start, end, kind))
    out: list[str] = []
    cursor = 0
    for start, end, kind in merged:
        out.append(text[cursor:start])
        marker = _POLICY.replacement_template.format(secret_type=kind)
        out.append(marker + "\n" * text.count("\n", start, end))
        cursor = end
    out.append(text[cursor:])
    return "".join(out), len(merged)


def is_sensitive(path: str) -> bool:
    """Files whose content is never shown to a model (keys, credentials, environment files)."""
    return is_sensitive_path(path, DEFAULT_SENSITIVE_PATH_PATTERNS)


def evidence_id(*parts: object) -> str:
    return "ev-" + hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()[:20]


def glob_regex(pattern: str) -> re.Pattern[str]:
    """A workspace glob as a regex: ``*`` stays inside a path segment, ``**`` crosses them."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = pattern.find("]", i + 2)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                body = pattern[i + 1 : j].replace("\\", "\\\\")
                out.append(f"[{'^' if body.startswith('!') else ''}{body.lstrip('!')}]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")
