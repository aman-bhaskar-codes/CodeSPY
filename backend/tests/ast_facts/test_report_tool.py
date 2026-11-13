# ruff: noqa: E501
"""read_report_section on PostgreSQL: pinned, scoped, paged, redacted, and every statement served leaves a receipt."""

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.contracts.investigation_execution import (
    InvestigationExecutionContextV1,
    InvestigationLimitsV1,
    InvestigationNodeKind,
    InvestigationScopeV1,
    InvestigationToolName,
    InvestigationToolRequestV1,
    InvestigationToolStatus,
)
from app.services.investigation_tools import InvestigationToolService, validate_tool_arguments
from sqlalchemy import text

from tests.ast_facts.test_report_narrative_schema import HEX, a_report, insert_spec

pytestmark = pytest.mark.skipif(
    os.environ.get("CODESPY_TEST_SCHEMA_TARGET") != "head",
    reason="requires the current schema head (revision 0049)",
)


async def seed(factory, ctx, snapshot, repo, ref, *, pinned=True, spec_hash=HEX):
    inv = uuid4()
    async with factory() as s, s.begin():
        if pinned:
            await insert_spec(
                s,
                ctx,
                snapshot,
                repo,
                spec_hash=spec_hash,
                report_id=ref.id,
                digest=ref.report_digest,
            )
        else:
            await insert_spec(
                s, ctx, snapshot, repo, spec_hash=spec_hash, mode="short_path", target="repository"
            )
        await s.execute(
            text(
                "INSERT INTO investigations(org_id,id,repository_id,principal_id,target_type,query,status,mode,depth,budget_limit_micros,spec_hash,snapshot_id)"
                " VALUES(:o,:i,:r,:p,:t,'q','pending',:m,'standard',1000,:h,:s)"
            ),
            {
                "o": ctx.org_id,
                "i": inv,
                "r": repo,
                "p": ctx.principal_id,
                "h": spec_hash,
                "s": snapshot,
                "t": "architecture_report" if pinned else "repository",
                "m": "report_narrative" if pinned else "short_path",
            },
        )
    return inv


def context(ctx, snapshot, repo, inv, spec_hash=HEX, org=None):
    return InvestigationExecutionContextV1(
        scope=InvestigationScopeV1(
            org_id=org or ctx.org_id,
            repository_id=repo,
            snapshot_id=snapshot,
            principal_id=ctx.principal_id,
            investigation_id=inv,
            spec_hash=spec_hash,
        ),
        logical_node_id=uuid4(),
        node_kind=InvestigationNodeKind.INVESTIGATE,
        role="narrator",
        logical_ordinal=1,
        attempt_job_id=uuid4(),
        attempt_id=uuid4(),
        attempt_fence=1,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        limits=InvestigationLimitsV1(),
    )


def request(section="overview", **extra):
    return InvestigationToolRequestV1(
        tool_name=InvestigationToolName.READ_REPORT_SECTION, arguments={"section": section, **extra}
    )


def test_arguments_are_strict():
    ok = validate_tool_arguments(InvestigationToolName.READ_REPORT_SECTION, {"section": "overview"})
    assert ok.valid and ok.cleaned == {"section": "overview", "offset": 0}
    for bad in (
        {},
        {"section": "nope"},
        {"section": "overview", "offset": -1},
        {"section": "overview", "offset": True},
        {"section": "overview", "report_id": "x"},
        {"section": "overview", "snapshot_id": "x"},
        {"section": 3},
    ):
        assert not validate_tool_arguments(InvestigationToolName.READ_REPORT_SECTION, bad).valid


async def test_a_page_of_statements_is_served_with_a_receipt_for_each(factory, tmp_path):
    ctx, snapshot, repo, ref = await a_report(factory, tmp_path)
    inv = await seed(factory, ctx, snapshot, repo, ref)
    tools = InvestigationToolService(session_factory=factory)
    assert [t.name for t in tools.get_report_tool_definitions()] == ["read_report_section"]
    assert "read_report_section" not in [t.name for t in tools.get_tool_definitions()]
    result = await tools.execute(request("overview"), context(ctx, snapshot, repo, inv))
    assert result.status in {InvestigationToolStatus.OK, InvestigationToolStatus.PARTIAL}
    page = json.loads(result.payload)
    assert (
        page["section"] == "overview"
        and page["total"] >= 1
        and len(result.evidence_refs) == len(page["statements"])
    )
    first = page["statements"][0]
    assert set(first) == {"sid", "kind", "code", "text", "params"} and len(first["sid"]) == 16
    async with factory() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT observation_type, fact_ref, source_path, excerpt_sha256 FROM investigation_evidence WHERE investigation_id=:i"
                ),
                {"i": inv},
            )
        ).all()
    assert len(rows) == len(page["statements"]) and all(
        r.observation_type == "report_fact" and r.source_path is None for r in rows
    )
    assert {r.fact_ref for r in rows} == {
        f"{ref.report_digest}:{e['sid']}" for e in page["statements"]
    }
    import hashlib

    assert {r.excerpt_sha256 for r in rows} == {hashlib.sha256(result.payload.encode()).hexdigest()}


async def test_paging_walks_a_section_without_gaps(factory, tmp_path):
    ctx, snapshot, repo, ref = await a_report(factory, tmp_path)
    inv = await seed(factory, ctx, snapshot, repo, ref)
    tools = InvestigationToolService(session_factory=factory)
    seen, offset = [], 0
    while True:
        out = await tools.execute(
            request("coverage", offset=offset), context(ctx, snapshot, repo, inv)
        )
        page = json.loads(out.payload)
        seen += [e["sid"] for e in page["statements"]]
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == page["total"]


async def test_the_tool_refuses_without_a_pin_across_tenants_and_after_the_report_changes(
    factory, tmp_path
):
    ctx, snapshot, repo, ref = await a_report(factory, tmp_path)
    tools = InvestigationToolService(session_factory=factory)
    unpinned = await seed(factory, ctx, snapshot, repo, ref, pinned=False, spec_hash="b" * 64)
    denied = await tools.execute(
        request(), context(ctx, snapshot, repo, unpinned, spec_hash="b" * 64)
    )
    assert (
        denied.status == InvestigationToolStatus.DENIED
        and denied.omission_reason == "no_pinned_report"
    )

    inv = await seed(factory, ctx, snapshot, repo, ref)
    other_org = await tools.execute(request(), context(ctx, snapshot, repo, inv, org=uuid4()))
    assert other_org.status == InvestigationToolStatus.DENIED
    other_snapshot = await tools.execute(request(), context(ctx, uuid4(), repo, inv))
    assert other_snapshot.status == InvestigationToolStatus.DENIED
    other_repo = await tools.execute(request(), context(ctx, snapshot, uuid4(), inv))
    assert other_repo.status == InvestigationToolStatus.DENIED

    unknown = await tools.execute(request("coverage"), context(ctx, snapshot, repo, inv))
    assert unknown.status in {InvestigationToolStatus.OK, InvestigationToolStatus.PARTIAL}


async def test_secret_looking_values_are_redacted_before_they_reach_the_model(
    factory, tmp_path, monkeypatch
):
    from app.services import investigation_tools as module

    ctx, snapshot, repo, ref = await a_report(factory, tmp_path)
    inv = await seed(factory, ctx, snapshot, repo, ref)
    secret = "AKIAIOSFODNN7EXAMPLE"  # noqa: S105 - a documented fake key used only to prove redaction
    real = module.statement_ids

    def tainted(report):
        rows = real(report)
        sid, statement = rows["overview"][0]
        rows["overview"][0] = (
            sid,
            statement.model_copy(update={"params": {**statement.params, "note": f"key {secret}"}}),
        )
        return rows

    monkeypatch.setattr(module, "statement_ids", tainted)
    result = await InvestigationToolService(session_factory=factory).execute(
        request("overview"), context(ctx, snapshot, repo, inv)
    )
    assert secret not in result.payload and "REDACTED" in result.payload
