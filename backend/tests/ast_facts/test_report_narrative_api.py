# ruff: noqa: E501
"""GET .../narrative: the latest published narrative of a report with the statements it cites."""

import os
from uuid import UUID, uuid4

import pytest
from app.analyzers.graph.statement_ids import statement_ids
from app.contracts.architecture_report import RepositoryReportV1
from app.contracts.report_narrative import (
    NarrativeDraftV1,
    NarrativeSentenceV1,
    NarrativeVerificationV1,
)
from app.services.jobs import JobService
from sqlalchemy import text

from tests.ast_facts.test_architecture_report_api import client_for
from tests.ast_facts.test_ast_facts_publication import operations
from tests.ast_facts.test_report_narrative_schema import HEX, insert_spec

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0049)",
)


async def built(client, headers, ctx, store, base, factory):
    shards = (await client.post(base + "/prepare", headers=headers)).json()
    lease = await JobService(factory).claim(
        ctx.org_id, UUID(shards["shards"][0]["job_id"]), 1, "worker"
    )
    await operations(factory, store)[2].execute(lease)
    ref = (await client.post(base, json={"request_key": "report-1"}, headers=headers)).json()
    report = RepositoryReportV1.model_validate_json((await client.get(f"{base}/{ref['id']}")).text)
    return ref, report


async def publish_narrative(factory, ctx, base_ids, ref, narrative):
    repo, snapshot = base_ids
    inv, rep = uuid4(), uuid4()
    async with factory() as s, s.begin():
        await insert_spec(
            s,
            ctx,
            snapshot,
            repo,
            spec_hash=HEX,
            report_id=UUID(ref["id"]),
            digest=ref["report_digest"],
        )
        await s.execute(
            text(
                "INSERT INTO investigations(org_id,id,repository_id,principal_id,target_type,query,status,mode,depth,budget_limit_micros,spec_hash,snapshot_id) VALUES(:o,:i,:r,:p,'architecture_report','q','pending','report_narrative','standard',1000,:h,:s)"
            ),
            {"o": ctx.org_id, "i": inv, "r": repo, "p": ctx.principal_id, "h": HEX, "s": snapshot},
        )
        await s.execute(text("ALTER TABLE investigation_reports DISABLE TRIGGER USER"))
        await s.execute(
            text(
                "INSERT INTO investigation_reports(org_id,id,investigation_id,spec_hash,job_id,verdict,verdict_confidence,summary,report_json,report_digest,claims_count,evidence_refs_count) VALUES(:o,:i,:v,:h,:j,'indeterminate',0,'s',CAST('{}' AS jsonb),:d,0,0)"
            ),
            {"o": ctx.org_id, "i": rep, "v": inv, "h": HEX, "j": uuid4(), "d": HEX},
        )
        await s.execute(
            text(
                "INSERT INTO architecture_report_narratives(org_id,id,architecture_report_id,repository_id,snapshot_id,investigation_id,investigation_report_id,spec_hash,report_digest,narrative_json) VALUES(:o,:i,:a,:r,:s,:v,:ir,:h,:d,CAST(:n AS jsonb))"
            ),
            {
                "o": ctx.org_id,
                "i": uuid4(),
                "a": UUID(ref["id"]),
                "r": repo,
                "s": snapshot,
                "v": inv,
                "ir": rep,
                "h": HEX,
                "d": ref["report_digest"],
                "n": narrative.model_dump_json(),
            },
        )
    return inv, rep


async def test_a_published_narrative_is_served_with_its_cited_statements(factory, tmp_path):
    client, headers, ctx, store, base = await client_for(factory, tmp_path)
    try:
        ref, report = await built(client, headers, ctx, store, base, factory)
        assert (
            await client.get(f"{base}/{ref['id']}/narrative")
        ).status_code == 404  # nothing published yet
        sid, statement = statement_ids(report)["overview"][0]
        narrative = NarrativeDraftV1(
            sentences=(
                NarrativeSentenceV1(claim_id="u0-c0", text="One file was analyzed.", sids=(sid,)),
            ),
            verifications=(
                NarrativeVerificationV1(claim_id="u0-c0", status="confirmed", basis="derived"),
            ),
            confirmed=1,
            total=1,
            grounded=True,
            unnarrated_sections=("coverage",),
            prompt_revision="report_narrative.v1.x",
            report_digest=ref["report_digest"],
        )
        repo = UUID(base.split("/repositories/")[1].split("/")[0])
        snapshot = UUID(base.split("/snapshots/")[1].split("/")[0])
        inv, rep = await publish_narrative(factory, ctx, (repo, snapshot), ref, narrative)
        found = await client.get(f"{base}/{ref['id']}/narrative")
        assert found.status_code == 200, found.text
        body = found.json()
        assert body["investigation_id"] == str(inv) and body["investigation_report_id"] == str(rep)
        assert body["narrative"]["confirmed"] == 1 and body["narrative"]["unnarrated_sections"] == [
            "coverage"
        ]
        assert [c["sid"] for c in body["cited"]] == [sid] and body["cited"][0][
            "section"
        ] == "overview"
        assert body["cited"][0]["kind"] == statement.kind and body["cited"][0]["text"]
        assert (
            await client.get(f"{base}/{uuid4()}/narrative")
        ).status_code == 404  # another report
    finally:
        await client.aclose()


async def test_the_narrative_route_needs_a_session(factory, tmp_path):
    client, _headers, _ctx, _store, base = await client_for(factory, tmp_path)
    try:
        client.cookies.clear()
        assert (await client.get(f"{base}/{uuid4()}/narrative")).status_code in {401, 403}
    finally:
        await client.aclose()
