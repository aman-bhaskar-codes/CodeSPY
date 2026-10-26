"""Trace persisted graph identities and source locations without inferring runtime behavior."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from app.contracts.trace import TraceBudgetV1
from app.domain.errors import CapacityExceeded
from app.models.phase3_tables import structural_reports
from app.models.scan_tables import finding_occurrences
from app.models.tables import analysis_runs, cpg_edges, cpg_nodes, source_files
from app.services.feature_trace import FeatureTraceService
from pydantic import ValidationError

from tests.phase4.unit.test_findings_authorization import AuthorityFixture
from tests.phase4.unit.test_findings_authorization import authority as authority_fixture

authority = authority_fixture


@pytest_asyncio.fixture
async def graph_case(authority: AuthorityFixture) -> tuple[AuthorityFixture, UUID]:
    run_id, old_run = uuid4(), uuid4()
    now = datetime.now(UTC)
    async with authority.factory() as session, session.begin():
        file_id = await session.scalar(
            sa.select(source_files.c.id).where(source_files.c.snapshot_id == authority.snapshot_id)
        )
        other_file = uuid4()
        await session.execute(
            source_files.insert().values(
                org_id=authority.ctx.org_id,
                snapshot_id=authority.snapshot_id,
                id=other_file,
                path="src/util.py",
                sha256="c" * 64,
                size_bytes=100,
            )
        )
        await session.execute(
            sa.update(finding_occurrences)
            .where(finding_occurrences.c.snapshot_id == authority.snapshot_id)
            .values(
                payload_json={
                    "title": "Fixture finding",
                    "entity": {
                        "range": {
                            "start_byte": 10,
                            "end_byte": 20,
                            "start_line": 10,
                            "end_line": 10,
                        }
                    },
                }
            )
        )
        for run, created_at in ((old_run, now - timedelta(seconds=1)), (run_id, now)):
            await session.execute(
                analysis_runs.insert().values(
                    org_id=authority.ctx.org_id,
                    snapshot_id=authority.snapshot_id,
                    id=run,
                    request_key=str(run),
                    analyzer_name="structural",
                    analyzer_version="1",
                    config_hash="a" * 64,
                    status="completed",
                    coverage={},
                    limitations=[],
                    result_hash="b" * 64,
                    started_at=created_at,
                    finished_at=created_at,
                    created_at=created_at,
                )
            )
            await session.execute(
                structural_reports.insert().values(
                    org_id=authority.ctx.org_id,
                    repository_id=authority.repository_id,
                    snapshot_id=authority.snapshot_id,
                    run_id=run,
                    spec_id=uuid4(),
                    config_hash="a" * 64,
                    report_json={"routes": [{"method": "GET", "raw_path": "/unrelated"}]},
                    report_sha256="b" * 64,
                    normalized_sha256="c" * 64,
                    created_at=created_at,
                )
            )
            for index in range(1, 5):
                await session.execute(
                    cpg_nodes.insert().values(
                        org_id=authority.ctx.org_id,
                        snapshot_id=authority.snapshot_id,
                        run_id=run,
                        local_id=UUID(f"aaaaaaaa-0000-0000-0000-{index:012x}"),
                        file_id=file_id if index == 1 else other_file,
                        node_kind="function",
                        symbol_name=f"{'old' if run == old_run else 'current'}_{index}",
                        qualified_name=f"module.function_{index}",
                        start_byte=0,
                        end_byte=100,
                        start_line=8 if index == 1 else 2,
                        end_line=20,
                        start_col=0,
                        end_col=1,
                        attributes={},
                    )
                )
            for index in range(1, 4):
                await session.execute(
                    cpg_edges.insert().values(
                        org_id=authority.ctx.org_id,
                        snapshot_id=authority.snapshot_id,
                        run_id=run,
                        id=uuid4(),
                        source_local_id=UUID(f"aaaaaaaa-0000-0000-0000-{index:012x}"),
                        target_local_id=UUID(f"aaaaaaaa-0000-0000-0000-{index + 1:012x}"),
                        edge_kind="CALLS_CANDIDATE",
                        support_evidence_id=uuid4(),
                        properties={},
                    )
                )
    return authority, run_id


async def test_trace_uses_one_published_run_and_actual_file_locations(graph_case) -> None:
    fixture, run_id = graph_case
    svc = FeatureTraceService(fixture.factory)
    trace = await svc.trace(
        fixture.ctx, fixture.repository_id, fixture.snapshot_id, fixture.finding_id
    )
    assert {node.label for node in trace.nodes} == {
        "Fixture finding",
        "current_1",
        "current_2",
        "current_3",
    }
    assert trace.hop_count == 2
    assert next(n for n in trace.nodes if n.label == "current_2").file_path == "src/util.py"
    assert next(n for n in trace.nodes if n.label == "current_2").line == 2
    assert all(n.kind != "route" for n in trace.nodes)
    assert trace.structural_run_id == run_id
    replay = await svc.trace(
        fixture.ctx, fixture.repository_id, fixture.snapshot_id, fixture.finding_id
    )
    assert replay.model_dump() == trace.model_dump()
    assert all(
        e.source_id in {n.id for n in trace.nodes} and e.target_id in {n.id for n in trace.nodes}
        for e in trace.edges
    )


@pytest.mark.parametrize(
    "budget,nodes,edges",
    [
        (TraceBudgetV1(max_nodes=1), 1, 0),
        (TraceBudgetV1(max_edges=1), 2, 1),
        (TraceBudgetV1(max_hops=0), 2, 1),
    ],
)
async def test_trace_counts_root_and_connection_against_limits(
    graph_case, budget, nodes, edges
) -> None:
    fixture, _ = graph_case
    trace = await FeatureTraceService(fixture.factory).trace(
        fixture.ctx, fixture.repository_id, fixture.snapshot_id, fixture.finding_id, budget
    )
    assert len(trace.nodes) == nodes
    assert len(trace.edges) == edges
    assert trace.hop_count <= budget.max_hops
    assert trace.budget_exceeded
    assert trace.omission_reasons


async def test_trace_without_containing_symbol_does_not_guess_nearest(graph_case) -> None:
    fixture, _ = graph_case
    async with fixture.factory() as session, session.begin():
        await session.execute(
            sa.update(finding_occurrences).values(
                payload_json={
                    "title": "Fixture finding",
                    "entity": {"range": {"start_line": 200, "end_line": 200}},
                }
            )
        )
    trace = await FeatureTraceService(fixture.factory).trace(
        fixture.ctx, fixture.repository_id, fixture.snapshot_id, fixture.finding_id
    )
    assert len(trace.nodes) == 1 and trace.edges == []
    assert "containing_symbol_unavailable" in trace.omission_reasons


async def test_trace_timeout_covers_authority_and_database_work(authority, monkeypatch) -> None:
    async def wait_for_authority(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("app.services.feature_trace.require_snapshot", wait_for_authority)
    with pytest.raises(CapacityExceeded):
        await asyncio.wait_for(
            FeatureTraceService(authority.factory).trace(
                authority.ctx,
                authority.repository_id,
                authority.snapshot_id,
                authority.finding_id,
                TraceBudgetV1(timeout_seconds=0.01),
            ),
            timeout=0.2,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_hops": -1},
        {"max_hops": 3},
        {"max_nodes": 0},
        {"max_nodes": 101},
        {"max_edges": -1},
        {"max_edges": 201},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
    ],
)
def test_trace_rejects_invalid_budgets(overrides) -> None:
    with pytest.raises(ValidationError):
        TraceBudgetV1(**overrides)


@pytest.mark.parametrize("body", ['{"max_hops": 3}', '{"max_nodes": -1}', "{invalid"])
async def test_trace_endpoint_rejects_invalid_budget(authority, monkeypatch, body) -> None:
    import httpx
    from app.api.application import ApiSettings, ApplicationPorts, create_app
    from app.security.sessions import SessionService

    async def fixture_now(_session):
        return datetime.now(UTC).replace(tzinfo=None)

    monkeypatch.setattr("app.security.sessions.database_now", fixture_now)
    issued = await SessionService(authority.factory).issue(authority.ctx.principal_id)
    app = create_app(
        ApiSettings(environment="test", auth_mode="local_fixture", origin="http://testserver"),
        ApplicationPorts(factory=authority.factory, store=None, fixture_context=authority.ctx),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies={"codespy_dev": issued.token, "codespy_dev_csrf": issued.csrf},
    ) as client:
        response = await client.post(
            f"/api/v1/repositories/{authority.repository_id}/snapshots/{authority.snapshot_id}/findings/{authority.finding_id}/trace",
            content=body,
            headers={"origin": "http://testserver", "x-csrf-token": issued.csrf},
        )
    assert response.status_code == 422
