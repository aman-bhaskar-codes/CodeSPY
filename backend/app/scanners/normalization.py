"""Reduce native reports to source-attested locations without retaining native text."""

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from app.analyzers.positions import SourceIndex
from app.contracts.scanners import ScannerObservationV1, ScannerRequestV1, ScannerResultV1
from app.domain.errors import PersistenceError


class ScannerOutputError(PersistenceError):
    def __init__(self) -> None:
        super().__init__("scanner_output_invalid")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ScannerOutputError()
        result[key] = value
    return result


def _source_indexes(
    request: ScannerRequestV1, sources: Mapping[UUID, bytes], aliases: Mapping[str, UUID]
) -> dict[UUID, SourceIndex]:
    expected = {file.file_id: file for file in request.files}
    if sources.keys() != expected.keys() or set(aliases.values()) != expected.keys():
        raise ScannerOutputError()
    if len(aliases) != len(expected):
        raise ScannerOutputError()
    for alias, file_id in aliases.items():
        if not re.fullmatch(file_id.hex + r"\.(?:py|js|jsx|ts|tsx|txt)", alias):
            raise ScannerOutputError()
    indexes = {}
    for file_id, data in sources.items():
        file = expected[file_id]
        if len(data) != file.size_bytes or hashlib.sha256(data).hexdigest() != file.sha256:
            raise ScannerOutputError()
        indexes[file_id] = SourceIndex(data)
    return indexes


def _failed(request: ScannerRequestV1) -> ScannerResultV1:
    return ScannerResultV1(
        request_digest=request.semantic_hash(),
        status="failed",
        processed_file_ids=(),
        omitted_file_ids=tuple(file.file_id for file in request.files),
    )


def normalize_semgrep(
    request: ScannerRequestV1,
    sources: Mapping[UUID, bytes],
    aliases: Mapping[str, UUID],
    raw: bytes,
    *,
    exit_code: int,
    expected_version: str,
) -> ScannerResultV1:
    """Accept only the fixed offline CLI profile; never propagate native diagnostics.

    Offsets and reported line/byte columns must independently agree with original
    UTF-8 bytes. Any native error invalidates the complete scan, including matches.
    Native compatibility still requires qualification against the pinned binary.
    """
    try:
        return _semgrep(request, sources, aliases, raw, exit_code, expected_version)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ScannerOutputError() from None


def _semgrep(
    request: ScannerRequestV1,
    sources: Mapping[UUID, bytes],
    aliases: Mapping[str, UUID],
    raw: bytes,
    exit_code: int,
    expected_version: str,
) -> ScannerResultV1:
    indexes = _source_indexes(request, sources, aliases)
    if request.profile.tool != "semgrep":
        raise ScannerOutputError()
    if exit_code != 0:
        return _failed(request)
    if len(raw) > request.profile.output_bytes:
        raise ScannerOutputError()
    document = json.loads(raw, object_pairs_hook=_unique_object)
    if document["version"] != expected_version or document.get("skipped_rules", []) != []:
        raise ScannerOutputError()
    if not isinstance(document["errors"], list):
        raise ScannerOutputError()
    if document["errors"]:
        return _failed(request)
    paths = document["paths"]
    scanned = paths["scanned"]
    if not isinstance(scanned, list) or len(scanned) != len(set(scanned)):
        raise ScannerOutputError()
    if not set(scanned) <= aliases.keys():
        raise ScannerOutputError()
    if paths.get("skipped", []):
        return _failed(request)
    findings = document["results"]
    if not isinstance(findings, list) or len(findings) > 1000:
        raise ScannerOutputError()
    observations = []
    for finding in findings:
        alias, rule = finding["path"], finding["check_id"]
        if alias not in scanned or rule not in request.profile.rule_ids:
            raise ScannerOutputError()
        file_id = aliases[alias]
        start, end = finding["start"], finding["end"]
        for point in (start, end):
            if any(type(point[key]) is not int for key in ("offset", "line", "col")):
                raise ScannerOutputError()
        span = indexes[file_id].range_from_bytes(start["offset"], end["offset"])
        if (
            (span.start_line, span.start_col + 1) != (start["line"], start["col"])
            or (span.end_line, span.end_col + 1) != (end["line"], end["col"])
            or span.start_byte == span.end_byte
        ):
            raise ScannerOutputError()
        observations.append(
            ScannerObservationV1(
                rule_id=rule,
                file_id=file_id,
                sha256=hashlib.sha256(sources[file_id]).hexdigest(),
                range=span,
            )
        )
    processed = {aliases[alias] for alias in scanned}
    result = ScannerResultV1(
        request_digest=request.semantic_hash(),
        status="completed" if processed == sources.keys() else "partial",
        processed_file_ids=tuple(f.file_id for f in request.files if f.file_id in processed),
        omitted_file_ids=tuple(f.file_id for f in request.files if f.file_id not in processed),
        observations=tuple(observations),
    )
    result.validate_for(request)
    return result


def normalize_gitleaks(
    request: ScannerRequestV1,
    sources: Mapping[UUID, bytes],
    aliases: Mapping[str, UUID],
    raw: bytes,
    *,
    exit_code: int,
) -> ScannerResultV1:
    """Normalize one verified alias from a fixed, single-file dir invocation.

    Coverage relies on the qualified private staging/command profile, not on a
    directory-wide success code. The only accepted predicate is the reviewed
    classic PAT-like format; no active credential claim or value hash is emitted.
    """
    try:
        return _gitleaks(request, sources, aliases, raw, exit_code)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError):
        raise ScannerOutputError() from None


def _gitleaks(
    request: ScannerRequestV1,
    sources: Mapping[UUID, bytes],
    aliases: Mapping[str, UUID],
    raw: bytes,
    exit_code: int,
) -> ScannerResultV1:
    indexes = _source_indexes(request, sources, aliases)
    if request.profile.tool != "gitleaks" or len(request.files) != 1:
        raise ScannerOutputError()
    if exit_code not in {0, 10}:
        return _failed(request)
    if len(raw) > request.profile.output_bytes:
        raise ScannerOutputError()
    findings = json.loads(raw, object_pairs_hook=_unique_object)
    if (
        not isinstance(findings, list)
        or len(findings) > 1000
        or bool(findings) != (exit_code == 10)
    ):
        raise ScannerOutputError()
    observations = []
    for finding in findings:
        alias, rule = finding["File"], finding["RuleID"]
        if alias not in aliases or rule not in request.profile.rule_ids:
            raise ScannerOutputError()
        if rule != "secret.github-classic-pat":
            raise ScannerOutputError()
        file_id = aliases[alias]
        data = sources[file_id]
        # Upstream 8.30.1 measures columns from the preceding newline byte,
        # with origin zero for the first line. Preserve CRLF and UTF-8 bytes.
        origins = [0] + [i for i, byte in enumerate(data) if byte == 10]
        for key in ("StartLine", "EndLine", "StartColumn", "EndColumn"):
            if type(finding[key]) is not int or finding[key] < 1:
                raise ScannerOutputError()
        if finding["StartLine"] != finding["EndLine"]:
            raise ScannerOutputError()
        origin = origins[finding["StartLine"] - 1]
        start = origin + finding["StartColumn"] - 1
        end = origin + finding["EndColumn"]
        span = indexes[file_id].range_from_bytes(start, end)
        if span.start_line != finding["StartLine"] or span.end_line != finding["EndLine"]:
            raise ScannerOutputError()
        if not re.fullmatch(rb"ghp_[A-Za-z0-9]{36}", data[start:end]):
            raise ScannerOutputError()
        # Reject a suffix/prefix match embedded inside a larger token.
        if (start and re.fullmatch(rb"[A-Za-z0-9_]", data[start - 1 : start])) or (
            end < len(data) and re.fullmatch(rb"[A-Za-z0-9_]", data[end : end + 1])
        ):
            raise ScannerOutputError()
        observations.append(
            ScannerObservationV1(
                rule_id=rule,
                file_id=file_id,
                sha256=hashlib.sha256(data).hexdigest(),
                range=span,
            )
        )
    result = ScannerResultV1(
        request_digest=request.semantic_hash(),
        status="completed",
        processed_file_ids=tuple(f.file_id for f in request.files),
        omitted_file_ids=(),
        observations=tuple(observations),
    )
    result.validate_for(request)
    return result
