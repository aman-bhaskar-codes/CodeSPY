"""Deterministic admitted selection and fail-closed runtime readiness."""

from uuid import uuid4

import pytest
from app.contracts.structure import SourceFileV1
from app.services.index_jobs import select_index_files
from tests.retrieval.unit.test_index_contracts import config


def source(path, size=0, eligible=True):
    return SourceFileV1(
        file_id=uuid4(),
        path=path,
        sha256="a" * 64,
        size_bytes=size,
        eligible=eligible,
        exclusion_reason=None if eligible else "binary",
    )


def test_selection_preserves_order_and_reports_each_omitted_file():
    values = (
        source("c.py", 2_097_153),
        source(".env"),
        source("b.bin", eligible=False),
        source("a.py", 1),
    )
    files, omissions = select_index_files(values, config())
    assert [f.path for f in files] == ["a.py"]
    assert {(x.reason, x.count) for x in omissions} == {("source_limit", 2), ("sensitive_path", 1)}
    assert select_index_files(tuple(reversed(values)), config()) == (files, omissions)


def test_selection_enforces_source_bytes_rows_and_metadata_budget():
    selected, omissions = select_index_files(
        tuple(source(f"{i:03d}.py", 2_097_152) for i in range(9)), config()
    )
    assert len(selected) == 8 and omissions[0].count == 1
    selected, omissions = select_index_files(
        tuple(source(f"{i:03d}.py") for i in range(129)), config()
    )
    assert len(selected) == 128 and omissions[0].count == 1
    selected, omissions = select_index_files(
        tuple(source("x" * 900 + f"{i:03d}.py") for i in range(128)), config()
    )
    assert len(selected) < 128 and sum(x.count for x in omissions) == 128 - len(selected)
    assert omissions[0].reason == "manifest_limit"


@pytest.mark.asyncio
async def test_unconfigured_builder_cannot_pass_job_readiness():
    from app.indexing.adapter import UnavailableIndexBuilder
    from app.ports.index_builder import IndexBuilderUnavailable

    with pytest.raises(IndexBuilderUnavailable):
        await UnavailableIndexBuilder().validate(config())


@pytest.mark.asyncio
async def test_runtime_config_must_match_actual_registered_image(tmp_path):
    from app.ports.index_builder import IndexBuilderUnavailable
    from tests.retrieval.unit.test_index_adapter import setup

    job, _, _, builder, _ = setup(tmp_path)
    changed = job.config.model_copy(update={"runtime_image": "sha256:" + "0" * 64})
    with pytest.raises(IndexBuilderUnavailable):
        await builder.validate(changed)


def test_legacy_http_job_contract_rejects_index_records():
    from datetime import UTC, datetime, timedelta

    from app.api.application import legacy_job_record
    from app.contracts.indexing import IndexJobRecordV5
    from fastapi import HTTPException

    now = datetime.now(UTC)
    record = IndexJobRecordV5(
        org_id=uuid4(),
        id=uuid4(),
        repository_id=uuid4(),
        snapshot_id=uuid4(),
        index_spec_id=uuid4(),
        state="queued",
        attempt_count=0,
        max_attempts=3,
        deadline=now + timedelta(minutes=15),
        failure_code=None,
        result_index_id=None,
        created_at=now,
        finished_at=None,
    )
    with pytest.raises(HTTPException) as caught:
        legacy_job_record(record)
    assert caught.value.status_code == 409 and caught.value.detail == "index_operation_unavailable"


@pytest.mark.asyncio
async def test_copied_invalid_config_is_revalidated_before_readiness_or_database():
    from app.domain.scope import OrgRole, TenantContext
    from app.services.index_jobs import IndexJobService

    class NoReadiness:
        async def validate(self, value):
            raise AssertionError("invalid input reached readiness")

    def no_database():
        raise AssertionError("invalid input reached database")

    invalid = config().model_copy(update={"max_tokens": 2048})
    ctx = TenantContext(org_id=uuid4(), principal_id=uuid4(), role=OrgRole.MEMBER)
    with pytest.raises(ValueError):
        await IndexJobService(no_database, NoReadiness()).create(
            ctx, uuid4(), uuid4(), invalid, "bad"
        )


def test_old_runtime_probe_without_request_budget_cannot_qualify_readiness():
    from app.ports.index_builder import IndexCapabilitiesV1

    with pytest.raises(ValueError):
        IndexCapabilitiesV1.model_validate_json('{"runtime_source_sha256":"' + "a" * 64 + '"}')
