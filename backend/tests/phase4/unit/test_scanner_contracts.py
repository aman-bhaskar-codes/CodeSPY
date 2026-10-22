"""Protect scanner trust boundaries; no scanner binary or service required."""

from copy import deepcopy
from uuid import UUID

import pytest
from app.contracts.scanners import ScannerRequestV1, ScannerResultV1
from pydantic import ValidationError

FILE = UUID(int=1)


def request_document():
    return {
        "scope": {"org_id": UUID(int=2), "snapshot_id": UUID(int=3), "run_id": UUID(int=4)},
        "repository_id": UUID(int=5),
        "config_hash": "a" * 64,
        "profile": {
            "tool": "semgrep",
            "artifact_sha256": "b" * 64,
            "rules_sha256": "c" * 64,
            "rule_ids": ("python.shell-true",),
        },
        "files": ({"file_id": FILE, "sha256": "d" * 64, "size_bytes": 100},),
    }


def result_document():
    return {
        "request_digest": ScannerRequestV1.model_validate(request_document()).semantic_hash(),
        "status": "completed",
        "processed_file_ids": (FILE,),
        "omitted_file_ids": (),
        "observations": (
            {
                "rule_id": "python.shell-true",
                "file_id": FILE,
                "sha256": "d" * 64,
                "range": {
                    "start_byte": 0,
                    "end_byte": 4,
                    "start_line": 1,
                    "end_line": 1,
                    "start_col": 0,
                    "end_col": 4,
                },
            },
        ),
    }


def test_completed_result_is_bound_to_selected_source_and_known_rules():
    request = ScannerRequestV1.model_validate(request_document())
    result = ScannerResultV1.model_validate(result_document())
    result.validate_for(request)
    assert result.observations[0].file_id == FILE


@pytest.mark.parametrize("change", ["rule", "file", "hash", "range", "duplicate", "unprocessed"])
def test_untrusted_observation_cannot_escape_request(change):
    data = deepcopy(result_document())
    observation = data["observations"][0]
    if change == "rule":
        observation["rule_id"] = "unknown.rule"
    elif change == "file":
        observation["file_id"] = UUID(int=99)
    elif change == "hash":
        observation["sha256"] = "e" * 64
    elif change == "range":
        observation["range"]["end_byte"] = 101
    elif change == "duplicate":
        data["observations"] *= 2
    elif change == "unprocessed":
        data["processed_file_ids"] = ()
        data["omitted_file_ids"] = (FILE,)
        data["status"] = "partial"
    with pytest.raises(ValueError):
        ScannerResultV1.model_validate(data).validate_for(
            ScannerRequestV1.model_validate(request_document())
        )


@pytest.mark.parametrize("field", ["message", "match", "secret", "snippet", "fingerprint"])
def test_raw_scanner_payload_is_rejected_without_echoing_secret(field):
    data = result_document()
    data["observations"][0][field] = "CANARY-PRIVATE-雪"
    with pytest.raises(ValidationError) as caught:
        ScannerResultV1.model_validate(data)
    assert "CANARY-PRIVATE-雪" not in str(caught.value)


@pytest.mark.parametrize("change", ["missing", "overlap", "foreign", "completed_omission"])
def test_coverage_cannot_hide_unprocessed_files(change):
    data = result_document()
    data["observations"] = ()
    if change == "missing":
        data["processed_file_ids"] = ()
    elif change == "overlap":
        data["omitted_file_ids"] = (FILE,)
    elif change == "foreign":
        data["processed_file_ids"] = (UUID(int=99),)
    else:
        data["processed_file_ids"] = ()
        data["omitted_file_ids"] = (FILE,)
    with pytest.raises(ValueError):
        ScannerResultV1.model_validate(data).validate_for(
            ScannerRequestV1.model_validate(request_document())
        )


def test_failed_scan_cannot_publish_observations_as_success():
    data = result_document()
    data["status"] = "failed"
    with pytest.raises(ValidationError):
        ScannerResultV1.model_validate(data)


@pytest.mark.parametrize("change", ["duplicate", "too_many", "too_large", "argv", "rules"])
def test_request_rejects_duplicate_unbounded_or_executable_inputs(change):
    data = request_document()
    if change == "duplicate":
        data["files"] *= 2
    elif change == "too_many":
        data["files"] = tuple(
            {"file_id": UUID(int=i + 100), "sha256": "d" * 64, "size_bytes": 1} for i in range(129)
        )
    elif change == "too_large":
        data["files"][0]["size_bytes"] = 2 * 1024 * 1024 + 1
    elif change == "argv":
        data["profile"]["argv"] = ("--config=https://untrusted.invalid",)
    else:
        data["profile"]["rule_ids"] = ("python.shell-true", "python.shell-true")
    with pytest.raises(ValidationError):
        ScannerRequestV1.model_validate(data)


def test_request_rejects_aggregate_source_limit():
    data = request_document()
    data["files"] = tuple(
        {"file_id": UUID(int=i + 100), "sha256": "d" * 64, "size_bytes": 2097152} for i in range(9)
    )
    with pytest.raises(ValidationError):
        ScannerRequestV1.model_validate(data)


def test_partial_result_explicitly_accounts_for_unprocessed_source():
    data = result_document()
    data.update(status="partial", processed_file_ids=(), omitted_file_ids=(FILE,), observations=())
    result = ScannerResultV1.model_validate(data)
    result.validate_for(ScannerRequestV1.model_validate(request_document()))
    assert result.status == "partial"


def test_profile_output_budget_is_enforced_before_publication():
    data = request_document()
    data["profile"]["output_bytes"] = 1
    request = ScannerRequestV1.model_validate(data)
    result = result_document()
    result["request_digest"] = request.semantic_hash()
    with pytest.raises(ValueError, match="output budget"):
        ScannerResultV1.model_validate(result).validate_for(request)


def test_duplicate_coverage_cannot_inflate_processed_counts():
    data = result_document()
    data["processed_file_ids"] = (FILE, FILE)
    with pytest.raises(ValidationError):
        ScannerResultV1.model_validate(data)


@pytest.mark.parametrize("change", ["run", "repository", "config", "artifact", "rules"])
def test_result_cannot_replay_across_request_authority(change):
    data = result_document()
    data["observations"] = ()
    result = ScannerResultV1.model_validate(data)
    other = request_document()
    if change == "run":
        other["scope"]["run_id"] = UUID(int=999)
    elif change == "repository":
        other["repository_id"] = UUID(int=999)
    elif change == "config":
        other["config_hash"] = "e" * 64
    elif change == "artifact":
        other["profile"]["artifact_sha256"] = "e" * 64
    else:
        other["profile"]["rules_sha256"] = "e" * 64
    with pytest.raises(ValueError, match="request identity"):
        result.validate_for(ScannerRequestV1.model_validate(other))
