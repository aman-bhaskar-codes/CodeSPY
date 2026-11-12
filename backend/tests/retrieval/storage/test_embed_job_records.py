"""Embed jobs project their own columns and produce a V7 record and queue envelope."""

import json
import os

import pytest
from app.contracts.embedding_jobs import EmbedJobRecordV7, JobEnvelopeV7
from app.repositories.job_repository import load_job, public_job_record
from app.repositories.outbox_repository import enqueue_wakeup
from sqlalchemy import text

from tests.retrieval.storage.test_dense_guards import embed_case

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0046)",
)


async def test_load_job_projects_the_embed_spec_and_builds_a_v7_record(
    api_factory, broker_factory, migration_factory, tmp_path
):
    case = await embed_case(api_factory, broker_factory, migration_factory, tmp_path)
    async with migration_factory() as session:
        row = await load_job(session, case["ctx"].org_id, case["binding"][0])
    assert row["operation"] == "embed_chunk_set"
    assert row["embed_spec_id"] == case["spec_id"] and row["state"] == "running"
    record = public_job_record(row)
    assert isinstance(record, EmbedJobRecordV7)
    assert record.embed_spec_id == case["spec_id"] and record.result_index_id is None


async def test_outbox_wakeup_for_an_embed_job_is_a_v7_envelope(
    api_factory, broker_factory, migration_factory, tmp_path
):
    case = await embed_case(api_factory, broker_factory, migration_factory, tmp_path)
    org = case["ctx"].org_id
    async with migration_factory() as session, session.begin():
        job = await load_job(session, org, case["binding"][0])
        before = await session.scalar(
            text("SELECT count(*) FROM outbox WHERE org_id=:o"), {"o": org}
        )
        await enqueue_wakeup(session, job)
        rows = (
            (
                await session.execute(
                    text("SELECT * FROM outbox WHERE org_id=:o ORDER BY created_at DESC LIMIT 1"),
                    {"o": org},
                )
            )
            .mappings()
            .all()
        )
        after = await session.scalar(
            text("SELECT count(*) FROM outbox WHERE org_id=:o"), {"o": org}
        )
    assert after == before + 1
    payload = rows[0]["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    envelope = JobEnvelopeV7.model_validate_json(json.dumps(payload))
    assert envelope.embed_spec_id == case["spec_id"] and envelope.operation == "embed_chunk_set"
    assert payload["schema_version"] == 7
