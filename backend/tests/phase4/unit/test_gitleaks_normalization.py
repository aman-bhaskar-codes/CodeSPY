"""Attest native locations against the selected format and original byte boundaries."""

import hashlib
import json
from uuid import UUID

import pytest
from app.contracts.scanners import ScannerRequestV1
from app.scanners.normalization import ScannerOutputError, normalize_gitleaks

FILE = UUID(int=1)
ALIAS = FILE.hex + ".txt"
TOKEN = b"ghp_" + b"A" * 36


def scan(data, start_line=1, start_col=1, end_col=40, *, exit_code=10, **changes):
    request = ScannerRequestV1.model_validate(
        {
            "scope": {"org_id": UUID(int=2), "snapshot_id": UUID(int=3), "run_id": UUID(int=4)},
            "repository_id": UUID(int=5),
            "config_hash": "a" * 64,
            "profile": {
                "tool": "gitleaks",
                "artifact_sha256": "b" * 64,
                "rules_sha256": "c" * 64,
                "rule_ids": ("secret.github-classic-pat",),
            },
            "files": (
                {
                    "file_id": FILE,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                },
            ),
        }
    )
    finding = {
        "RuleID": "secret.github-classic-pat",
        "File": ALIAS,
        "StartLine": start_line,
        "EndLine": start_line,
        "StartColumn": start_col,
        "EndColumn": end_col,
        "Secret": "REDACTED",
        "Match": "REDACTED",
        "Description": "PRIVATE-CANARY",
        "Fingerprint": "PRIVATE-CANARY",
    }
    finding.update(changes)
    return normalize_gitleaks(
        request, {FILE: data}, {ALIAS: FILE}, json.dumps([finding]).encode(), exit_code=exit_code
    )


@pytest.mark.parametrize(
    "prefix,line,column",
    [
        (b"", 1, 1),
        ("雪 ".encode(), 1, 5),
        (b"first\r\n", 2, 2),
        (b"first\n", 2, 2),
        (b"first\n  ", 2, 4),
    ],
)
def test_native_newline_origin_does_not_shift_authoritative_range(prefix, line, column):
    result = scan(prefix + TOKEN, line, column, column + 39)
    assert result.observations[0].range.start_byte == len(prefix)
    assert result.observations[0].range.end_byte == len(prefix) + len(TOKEN)
    assert "PRIVATE-CANARY" not in result.model_dump_json()
    assert TOKEN.decode() not in result.model_dump_json()


@pytest.mark.parametrize(
    "changes",
    [
        {"StartColumn": 0},
        {"StartColumn": True},
        {"EndColumn": 1000},
        {"File": "../outside"},
        {"RuleID": "unknown"},
        {"EndLine": 2},
    ],
)
def test_invalid_native_location_or_identity_fails_closed(changes):
    with pytest.raises(ScannerOutputError):
        scan(TOKEN, **changes)


def test_valid_range_without_the_reviewed_format_cannot_support_a_secret_observation():
    with pytest.raises(ScannerOutputError):
        scan(b"x" * 40)


def test_partial_scan_error_is_rejected_even_when_report_contains_valid_findings():
    result = scan(TOKEN, exit_code=1)
    assert result.status == "failed"
    assert result.observations == ()
    assert result.omitted_file_ids == (FILE,)
