from uuid import uuid4

import pytest
from app.contracts.memory import EvidencePointerV1, MemoryCreateRequestV1, MemoryKind
from pydantic import ValidationError


def test_proposals_bound_evidence_fanout():
    pointer = EvidencePointerV1(file_path="a.py", start_line=1, end_line=1)
    with pytest.raises(ValidationError):
        MemoryCreateRequestV1(
            memory_kind=MemoryKind.ARCHITECTURE_FACT,
            topic="a",
            summary="a",
            evidence_pointers=(pointer,) * 11,
        )


def test_review_requires_expected_version_and_bounded_reason():
    from app.contracts.memory import MemoryReviewRequestV1

    with pytest.raises(ValidationError):
        MemoryReviewRequestV1(action="accept", expected_version=0, request_key=str(uuid4()))


def test_proposals_reject_reversed_evidence_range():
    with pytest.raises(ValidationError):
        EvidencePointerV1(file_path="a.py", start_line=4, end_line=1)


def test_legacy_job_transport_rejects_investigation_jobs():
    from app.api.application import legacy_job_record
    from app.contracts.investigation_jobs import InvestigationJobRecordV1
    from fastapi import HTTPException

    record = InvestigationJobRecordV1.model_construct(operation="investigate_snapshot")
    with pytest.raises(HTTPException) as error:
        legacy_job_record(record)
    assert error.value.status_code == 409
    assert error.value.detail == "investigation_operation_unavailable"
