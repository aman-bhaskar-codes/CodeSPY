"""Embed job contracts: key rules, bounded envelope, consistent terminal records."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.contracts.embedding_jobs import EmbedJobRecordV7, EnqueueEmbedJobV7, JobEnvelopeV7
from pydantic import ValidationError

NOW = datetime.now(UTC)


def enqueue(**overrides):
    values = {
        "repository_id": uuid4(),
        "snapshot_id": uuid4(),
        "embed_spec_id": uuid4(),
        "config_hash": "a" * 64,
        "request_key": "embed-1",
    }
    return EnqueueEmbedJobV7(**{**values, **overrides})


def record(**overrides):
    values = {
        "org_id": uuid4(),
        "id": uuid4(),
        "repository_id": uuid4(),
        "snapshot_id": uuid4(),
        "embed_spec_id": uuid4(),
        "state": "queued",
        "attempt_count": 0,
        "max_attempts": 3,
        "deadline": NOW + timedelta(minutes=15),
        "failure_code": None,
        "result_index_id": None,
        "created_at": NOW,
        "finished_at": None,
    }
    return EmbedJobRecordV7(**{**values, **overrides})


def test_request_keys_cannot_use_reserved_namespaces_or_exceed_the_bound():
    assert enqueue().operation == "embed_chunk_set"
    for key in ("job:1", "child:1", "k" * 129, ""):
        with pytest.raises(ValidationError):
            enqueue(request_key=key)


def test_envelope_carries_identity_only_and_stays_small():
    envelope = JobEnvelopeV7(
        org_id=uuid4(),
        job_id=uuid4(),
        embed_spec_id=uuid4(),
        config_hash="b" * 64,
        dispatch_generation=1,
        deadline=NOW,
    )
    assert envelope.schema_version == 7
    assert set(envelope.model_dump()) == {
        "schema_version",
        "org_id",
        "job_id",
        "embed_spec_id",
        "operation",
        "config_hash",
        "dispatch_generation",
        "deadline",
    }
    with pytest.raises(ValidationError):
        JobEnvelopeV7(**{**envelope.model_dump(), "operation": "index_snapshot"})


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": "succeeded"},  # terminal needs finished_at and a result index
        {"state": "succeeded", "finished_at": NOW},  # success needs a result index
        {"state": "failed", "result_index_id": uuid4(), "finished_at": NOW},
        {"attempt_count": 4},
    ],
)
def test_inconsistent_records_are_rejected(overrides):
    with pytest.raises(ValidationError):
        record(**overrides)


def test_consistent_terminal_records_are_accepted():
    assert record(state="succeeded", result_index_id=uuid4(), finished_at=NOW).state == "succeeded"
    assert record(state="failed", failure_code="embed_timeout", finished_at=NOW).failure_code
