"""Native reports are untrusted; only authenticated metadata may leave isolation."""

import hashlib
import json
from uuid import UUID

import pytest
from app.analyzers.positions import SourceIndex
from app.contracts.scanners import ScannerRequestV1
from app.scanners.normalization import ScannerOutputError, normalize_semgrep

FILE = UUID(int=1)
CANARY = "PRIVATE-SYNTHETIC-雪"
DATA = "雪 = 1\r\nrun(command, shell=True)\n".encode()
ALIAS = FILE.hex + ".py"


def request():
    return ScannerRequestV1.model_validate(
        {
            "scope": {"org_id": UUID(int=2), "snapshot_id": UUID(int=3), "run_id": UUID(int=4)},
            "repository_id": UUID(int=5),
            "config_hash": "a" * 64,
            "profile": {
                "tool": "semgrep",
                "artifact_sha256": "b" * 64,
                "rules_sha256": "c" * 64,
                "rule_ids": ("python.shell-true",),
            },
            "files": (
                {
                    "file_id": FILE,
                    "sha256": hashlib.sha256(DATA).hexdigest(),
                    "size_bytes": len(DATA),
                },
            ),
        }
    )


def report():
    span = SourceIndex(DATA).range_from_bytes(DATA.index(b"run("), len(DATA) - 1)
    return {
        "version": "1.179.0",
        "errors": [],
        "skipped_rules": [],
        "paths": {"scanned": [ALIAS]},
        "results": [
            {
                "check_id": "python.shell-true",
                "path": ALIAS,
                "start": {
                    "line": span.start_line,
                    "col": span.start_col + 1,
                    "offset": span.start_byte,
                },
                "end": {"line": span.end_line, "col": span.end_col + 1, "offset": span.end_byte},
                "extra": {"message": CANARY, "lines": CANARY, "metavars": {"$X": CANARY}},
            }
        ],
    }


def normalize(document=None, **kwargs):
    return normalize_semgrep(
        request(),
        {FILE: DATA},
        {ALIAS: FILE},
        json.dumps(document or report()).encode(),
        exit_code=0,
        expected_version="1.179.0",
        **kwargs,
    )


def test_authenticates_original_utf8_bytes_and_discards_native_text():
    result = normalize()
    result.validate_for(request())
    assert result.status == "completed"
    assert result.processed_file_ids == (FILE,)
    assert result.observations[0].range.start_byte == DATA.index(b"run(")
    assert CANARY not in result.model_dump_json()
    assert "extra" not in result.model_dump_json()


@pytest.mark.parametrize(
    "change",
    [
        "rule",
        "path",
        "offset",
        "column",
        "unicode",
        "duplicate",
        "coverage",
        "version",
        "skipped_rule",
        "type",
    ],
)
def test_invalid_native_identity_or_location_fails_closed_without_native_text(change):
    document = report()
    finding = document["results"][0]
    if change == "rule":
        finding["check_id"] = CANARY
    elif change == "path":
        finding["path"] = "../" + CANARY
    elif change == "offset":
        finding["end"]["offset"] = len(DATA) + 1
    elif change == "column":
        finding["start"]["col"] += 1
    elif change == "unicode":
        finding["start"] = {"line": 1, "col": 2, "offset": 1}
    elif change == "duplicate":
        document["results"] *= 2
    elif change == "coverage":
        document["paths"]["scanned"] = []
    elif change == "version":
        document["version"] = "other"
    elif change == "skipped_rule":
        document["skipped_rules"] = [CANARY]
    elif change == "type":
        finding["start"]["offset"] = True
    with pytest.raises(ScannerOutputError) as caught:
        normalize(document)
    assert CANARY not in str(caught.value)


def test_report_errors_cannot_produce_a_clean_result():
    document = report()
    document["errors"] = [{"message": CANARY}]
    result = normalize(document)
    assert result.status == "failed"
    assert result.observations == ()
    assert result.processed_file_ids == ()
    assert result.omitted_file_ids == (FILE,)


@pytest.mark.parametrize("data", [DATA + b" ", b"x" * len(DATA), b"\xff" * len(DATA)])
def test_source_bytes_must_match_request(data):
    with pytest.raises(ScannerOutputError):
        normalize_semgrep(
            request(),
            {FILE: data},
            {ALIAS: FILE},
            json.dumps(report()).encode(),
            exit_code=0,
            expected_version="1.179.0",
        )


@pytest.mark.parametrize(
    "raw",
    [b"[]", b"{", b"{" + b" " * 2097152, b'{"version":1,"version":2}'],
    ids=["array", "truncated", "oversized", "duplicate-key"],
)
def test_malformed_or_oversized_report_has_fixed_error(raw):
    with pytest.raises(ScannerOutputError) as caught:
        normalize_semgrep(
            request(), {FILE: DATA}, {ALIAS: FILE}, raw, exit_code=0, expected_version="1.179.0"
        )
    assert str(caught.value) == "scanner_output_invalid"


def test_nonzero_tool_failure_omits_all_without_findings():
    result = normalize_semgrep(
        request(), {FILE: DATA}, {ALIAS: FILE}, b"", exit_code=2, expected_version="1.179.0"
    )
    assert result.status == "failed"
    assert result.observations == ()
    assert result.omitted_file_ids == (FILE,)
