"""Connected SQL authority tests; index preparation is explicitly synthetic."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from app.domain.errors import ScopeViolation
from app.mcp.runtime import ConnectedMcpServer
from app.mcp.server import ReadOnlyMcpServer
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import repositories
from sqlalchemy import insert, text

BACKEND_DIR = Path(__file__).resolve().parents[3]

pytestmark = [
    pytest.mark.schema_revision("0040_investigation_runtime_permissions"),
    pytest.mark.skipif(
        not os.environ.get("CODESPY_TEST_MARKER"),
        reason="owned disposable PostgreSQL environment required",
    ),
]


async def test_connected_search_context_memory_and_revocation(
    api_factory,
    broker_factory,
    migration_factory,
    tmp_path,
):
    from tests.retrieval.storage.test_index_search import published

    ctx, scope, receipt = await published(
        api_factory,
        broker_factory,
        migration_factory,
        tmp_path,
    )
    facade = ConnectedMcpServer(
        identity=ctx, repository_allowlist=frozenset({scope.repository_id}), factory=api_factory
    )
    args = {"repository_id": str(scope.repository_id), "snapshot_id": str(scope.snapshot_id)}
    exact = json.loads(await facade.dispatch_tool("search_exact", {**args, "query": "a.py"}))
    assert exact["chunk_set_id"] == str(receipt.chunk_set_id)
    hybrid = json.loads(await facade.dispatch_tool("search_hybrid", {**args, "query": "print"}))
    assert hybrid["hits"] and hybrid["fallback_mode"] == "lexical_only"
    context = json.loads(await facade.dispatch_tool("assemble_context", {**args, "query": "print"}))
    assert context["chunk_set_id"] == exact["chunk_set_id"]
    assert context["items"][0]["safe_content"] == "print(1)\n"

    # Index status check
    status = json.loads(
        await facade.dispatch_tool("get_index_status", {**args, "job_id": str(receipt.job_id)})
    )
    assert status["job"]["id"] == str(receipt.job_id)
    assert status["job"]["state"] == "succeeded"
    with pytest.raises(ScopeViolation):
        await facade.dispatch_tool("get_index_status", {**args, "job_id": str(uuid4())})

    # Memory creation, review and query
    from app.contracts.memory import (
        MemoryCreateRequestV1,
        MemoryKind,
        MemoryReviewRequestV1,
    )
    from app.models.tables import memberships
    from app.services.memory_service import MemoryService

    mem_req = MemoryCreateRequestV1(
        org_id=ctx.org_id,
        repository_id=scope.repository_id,
        snapshot_id=scope.snapshot_id,
        principal_id=ctx.principal_id,
        memory_kind=MemoryKind.ARCHITECTURE_FACT,
        topic="adapters",
        summary="Verified adapter structure",
        request_key="mcp_conn_key",
    )
    async with api_factory() as s, s.begin():
        created_mem = await MemoryService(s).record_memory(mem_req)

    # Elevation to admin role for memory review
    async with migration_factory() as s, s.begin():
        await s.execute(
            memberships.update().where(memberships.c.org_id == ctx.org_id).values(role="admin")
        )
    async with api_factory() as s, s.begin():
        await MemoryService(s).review_memory(
            ctx.org_id,
            scope.repository_id,
            ctx.principal_id,
            created_mem.memory_id,
            MemoryReviewRequestV1(
                action="accept",
                expected_version=1,
                request_key="rev_conn",
                reason="Approved",
            ),
        )

    memories = json.loads(await facade.dispatch_tool("query_memory", {**args, "query": "adapter"}))
    assert len(memories["items"]) == 1
    assert memories["items"][0]["memory_id"] == str(created_mem.memory_id)

    # Expiry exclusion
    async with migration_factory() as s, s.begin():
        await s.execute(text("ALTER TABLE repo_memories DISABLE TRIGGER guard_memory_projection"))
        await s.execute(
            text(
                "UPDATE repo_memories SET "
                "expires_at=clock_timestamp()-interval '1 second', "
                "purge_after=clock_timestamp()-interval '1 second'"
            )
        )
        await s.execute(text("ALTER TABLE repo_memories ENABLE TRIGGER guard_memory_projection"))
    memories_expired = json.loads(
        await facade.dispatch_tool("query_memory", {**args, "query": "adapter"})
    )
    assert len(memories_expired["items"]) == 0

    # Investigation status and foreign rejection
    from app.contracts.investigation import InvestigationDepth, InvestigationMode
    from app.contracts.investigation_jobs import InvestigationCreateRequestV2
    from app.services.investigation_jobs import InvestigationJobService

    inv_service = InvestigationJobService(api_factory)
    inv = await inv_service.create(
        ctx,
        InvestigationCreateRequestV2(
            repository_id=scope.repository_id,
            snapshot_id=scope.snapshot_id,
            target_type="repository",
            query="Analyze entrypoint invariants",
            mode=InvestigationMode.SHORT_PATH,
            depth=InvestigationDepth.STANDARD,
            budget_limit_micros=1000000,
        ),
        request_key=uuid4().hex,
    )
    inv_status = json.loads(
        await facade.dispatch_tool(
            "get_investigation_status",
            {
                "repository_id": str(scope.repository_id),
                "investigation_id": str(inv.id),
            },
        )
    )
    assert inv_status["investigation_id"] == str(inv.id)
    assert inv_status["status"] in ("pending", "queued", "running")

    inv_result = json.loads(
        await facade.dispatch_tool(
            "get_investigation_result",
            {
                "repository_id": str(scope.repository_id),
                "investigation_id": str(inv.id),
            },
        )
    )
    assert inv_result["investigation_id"] == str(inv.id)
    assert inv_result["status"] in ("pending", "queued", "running")

    claims_result = json.loads(
        await facade.dispatch_tool(
            "list_claims",
            {
                "repository_id": str(scope.repository_id),
                "investigation_id": str(inv.id),
            },
        )
    )
    assert claims_result["investigation_id"] == str(inv.id)
    assert claims_result["claims"] == []

    with pytest.raises(ScopeViolation):
        await facade.dispatch_tool(
            "get_investigation_status",
            {
                "repository_id": str(scope.repository_id),
                "investigation_id": str(uuid4()),
            },
        )

    with pytest.raises(ScopeViolation):
        await facade.dispatch_tool(
            "get_investigation_result",
            {
                "repository_id": str(scope.repository_id),
                "investigation_id": str(uuid4()),
            },
        )

    with pytest.raises(ScopeViolation):
        await facade.dispatch_tool(
            "list_claims",
            {
                "repository_id": str(scope.repository_id),
                "investigation_id": str(uuid4()),
            },
        )

    grants = PHASE2_TABLES["repository_grants"]
    async with migration_factory() as s, s.begin():
        await s.execute(
            grants.update()
            .where(
                grants.c.org_id == ctx.org_id,
                grants.c.repository_id == scope.repository_id,
                grants.c.principal_id == ctx.principal_id,
            )
            .values(revoked_at=text("clock_timestamp()"))
        )
    with pytest.raises(ScopeViolation):
        await facade.dispatch_tool("search_exact", {**args, "query": "a.py"})


@pytest.mark.parametrize("tool", ["get_evidence", "graph_neighbors"])
async def test_repository_snapshot_mismatch_denied_by_direct_sql_adapters(
    api_factory,
    broker_factory,
    migration_factory,
    tmp_path,
    tool,
):
    from tests.retrieval.storage.test_index_search import published

    ctx, scope, _ = await published(api_factory, broker_factory, migration_factory, tmp_path)
    other_repo = uuid4()
    async with migration_factory() as s, s.begin():
        await s.execute(
            insert(repositories).values(
                org_id=ctx.org_id,
                id=other_repo,
                repository_key="other-" + other_repo.hex,
                display_name="Other",
                source_kind="archive",
            )
        )
        await s.execute(
            insert(PHASE2_TABLES["repository_grants"]).values(
                org_id=ctx.org_id,
                repository_id=other_repo,
                principal_id=ctx.principal_id,
                permission="read",
            )
        )
    facade = ReadOnlyMcpServer(
        identity=ctx, db_factory=api_factory, repository_allowlist=frozenset({other_repo})
    )
    args = {"repository_id": str(other_repo), "snapshot_id": str(scope.snapshot_id)}
    args["evidence_id" if tool == "get_evidence" else "node_id"] = str(uuid4())
    with pytest.raises(ScopeViolation):
        await facade.dispatch_tool(tool, args)


async def test_cancelled_database_call_releases_connection(api_factory):
    backend_pid = asyncio.Queue()

    async def slow_read():
        async with api_factory() as session, session.begin():
            await backend_pid.put(await session.scalar(text("SELECT pg_backend_pid()")))
            await session.execute(text("SELECT pg_sleep(30)"))

    task = asyncio.create_task(slow_read())
    pid = await backend_pid.get()
    try:
        # Coordinate on observed database work rather than an arbitrary delay.
        async with api_factory() as probe, asyncio.timeout(5):
            while not await probe.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                    "WHERE pid=:pid AND wait_event='PgSleep')"
                ),
                {"pid": pid},
            ):
                await probe.rollback()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with api_factory() as session:
        assert await session.scalar(text("SELECT 1")) == 1


async def test_sdk_source_preview_with_isolated_reader_is_database_read_only(
    api_factory,
    broker_factory,
    migration_factory,
    db_values,
    tmp_path,
    monkeypatch,
):
    image = os.environ.get("CODESPY_SOURCE_READER_IMAGE")
    executable = os.environ.get("CODESPY_MCP_DOCKER_EXECUTABLE")
    socket = os.environ.get("CODESPY_MCP_DOCKER_SOCKET")
    if not all((image, executable, socket)):
        pytest.skip("qualified source image and explicit local Docker paths required")
    import anyio
    from app.mcp.server import create_mcp_server
    from app.mcp.source_runtime import source_resources
    from app.models.derived_artifacts import derived_artifacts
    from app.models.tables import source_files
    from mcp.client.session import ClientSession
    from scripts import phase2_db as harness
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from tests.mcp.unit.test_mcp_sdk_client import _create_stream_pair
    from tests.retrieval.storage.test_index_search import published

    ctx, scope, _ = await published(api_factory, broker_factory, migration_factory, tmp_path)
    async with api_factory() as session:
        file_id = await session.scalar(
            select(source_files.c.id).where(
                source_files.c.org_id == ctx.org_id,
                source_files.c.snapshot_id == scope.snapshot_id,
                source_files.c.path == "a.py",
            )
        )
        before = await session.scalar(select(func.count()).select_from(derived_artifacts))
    monkeypatch.setenv("CODESPY_SOURCE_ROOT", str(tmp_path / "objects"))
    monkeypatch.setenv("CODESPY_MCP_SOURCE_IMAGE", image)
    url = harness.url(
        db_values,
        db_values["CODESPY_TEST_DATABASE"],
        "p2_api",
        db_values["CODESPY_TEST_APP_PASSWORD"],
        driver="postgresql+asyncpg",
    )
    engine = create_async_engine(
        url,
        connect_args={
            "server_settings": {"default_transaction_read_only": "on"},
        },
    )
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with source_resources() as (store, reader):
            facade = ConnectedMcpServer(
                identity=ctx,
                repository_allowlist=frozenset({scope.repository_id}),
                factory=factory,
                store=store,
                reader=reader,
            )
            server = create_mcp_server(facade)
            client_send, server_receive, server_send, client_receive = _create_stream_pair()
            async with anyio.create_task_group() as group:
                group.start_soon(
                    server.run, server_receive, server_send, server.create_initialization_options()
                )
                async with ClientSession(client_receive, client_send) as client:
                    await client.initialize()
                    result = await client.call_tool(
                        "get_source_view",
                        {
                            "repository_id": str(scope.repository_id),
                            "snapshot_id": str(scope.snapshot_id),
                            "file_id": str(file_id),
                        },
                    )
                    assert not result.is_error
                    assert result.structured_content["content_text"] == "print(1)\n"
                group.cancel_scope.cancel()
        async with api_factory() as session:
            assert (
                await session.scalar(select(func.count()).select_from(derived_artifacts)) == before
            )
    finally:
        await engine.dispose()


async def test_connected_findings_and_feature_trace(
    api_factory,
    broker_factory,
    migration_factory,
    tmp_path,
):
    from app.services.scan_publication import FindingFinalizer
    from tests.phase4.storage.test_scan_publication import publication_case

    ctx, spec, _, lease, draft = await publication_case(
        api_factory, broker_factory, migration_factory, tmp_path
    )
    await FindingFinalizer(broker_factory).finish(lease, draft)
    repo_id = spec.repository_id
    snap_id = lease.run_ref.snapshot_id
    run_id = lease.run_ref.run_id
    finding_id = draft.findings[0].id

    facade = ConnectedMcpServer(
        identity=ctx,
        repository_allowlist=frozenset({repo_id}),
        factory=api_factory,
    )
    findings_raw = await facade.dispatch_tool(
        "list_findings",
        {
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "run_id": str(run_id),
        },
    )
    findings_res = json.loads(findings_raw)
    assert len(findings_res["findings"]) > 0
    assert findings_res["findings"][0]["id"] == str(finding_id)
    assert findings_res["findings"][0]["current_support"] is not None

    trace_raw = await facade.dispatch_tool(
        "trace_feature",
        {
            "repository_id": str(repo_id),
            "snapshot_id": str(snap_id),
            "finding_id": str(finding_id),
            "max_hops": 2,
        },
    )
    trace_res = json.loads(trace_raw)
    assert trace_res["finding_id"] == str(finding_id)


async def test_connected_structural_graph_and_evidence(
    api_factory,
    broker_factory,
    migration_factory,
    tmp_path,
):
    from app.models.tables import cpg_nodes, evidence_records
    from sqlalchemy import select
    from tests.phase3.integration.test_structural_publication import (
        FixtureRunner,
        StructuralAnalysisOperation,
        StructuralFinalizer,
        structural,
    )

    ctx, spec, _job, lease, store = await structural(migration_factory, tmp_path)
    finalizer = StructuralFinalizer(broker_factory, store)
    await StructuralAnalysisOperation(broker_factory, store, finalizer, FixtureRunner()).execute(
        lease
    )
    repo_id = spec.repository_id
    snap_id = lease.run_ref.snapshot_id
    run_id = lease.run_ref.run_id

    async with api_factory() as session:
        node_id = await session.scalar(
            select(cpg_nodes.c.local_id).where(cpg_nodes.c.run_id == run_id).limit(1)
        )
        ev_id = await session.scalar(
            select(evidence_records.c.id).where(evidence_records.c.run_id == run_id).limit(1)
        )

    facade = ConnectedMcpServer(
        identity=ctx,
        repository_allowlist=frozenset({repo_id}),
        factory=api_factory,
        store=store,
    )
    if node_id:
        graph_res = json.loads(
            await facade.dispatch_tool(
                "graph_neighbors",
                {
                    "repository_id": str(repo_id),
                    "snapshot_id": str(snap_id),
                    "node_id": str(node_id),
                    "structural_run_id": str(run_id),
                },
            )
        )
        assert graph_res["root_node_id"] == str(node_id)
        assert len(graph_res["nodes"]) >= 1

    if ev_id:
        ev_res = json.loads(
            await facade.dispatch_tool(
                "get_evidence",
                {
                    "repository_id": str(repo_id),
                    "snapshot_id": str(snap_id),
                    "evidence_id": str(ev_id),
                },
            )
        )
        assert ev_res["id"] == str(ev_id)
        assert "raw_payload_omitted" in ev_res["limitations"]


async def test_subprocess_connected_with_real_postgres(
    api_factory,
    broker_factory,
    migration_factory,
    db_values,
    tmp_path,
):
    import sys

    from app.contracts.investigation import InvestigationDepth, InvestigationMode
    from app.contracts.investigation_jobs import InvestigationCreateRequestV2
    from app.models.tables import source_files
    from app.services.investigation_jobs import InvestigationJobService
    from scripts import phase2_db as harness
    from sqlalchemy import select
    from tests.retrieval.storage.test_index_search import published

    ctx, scope, receipt = await published(
        api_factory,
        broker_factory,
        migration_factory,
        tmp_path,
    )
    async with api_factory() as s:
        file_id = await s.scalar(
            select(source_files.c.id).where(
                source_files.c.snapshot_id == scope.snapshot_id,
                source_files.c.path == "a.py",
            )
        )

    inv_service = InvestigationJobService(api_factory)
    inv = await inv_service.create(
        ctx,
        InvestigationCreateRequestV2(
            repository_id=scope.repository_id,
            snapshot_id=scope.snapshot_id,
            target_type="repository",
            query="Subprocess test query",
            mode=InvestigationMode.SHORT_PATH,
            depth=InvestigationDepth.STANDARD,
            budget_limit_micros=1000000,
        ),
        request_key=uuid4().hex,
    )

    url = harness.url(
        db_values,
        db_values["CODESPY_TEST_DATABASE"],
        "p2_api",
        db_values["CODESPY_TEST_APP_PASSWORD"],
        driver="postgresql+asyncpg",
    )
    env = os.environ.copy()
    env["CODESPY_MCP_ORG_ID"] = str(ctx.org_id)
    env["CODESPY_MCP_PRINCIPAL_ID"] = str(ctx.principal_id)
    env["CODESPY_MCP_ROLE"] = "member"
    env["CODESPY_MCP_REPOSITORY_ALLOWLIST"] = str(scope.repository_id)
    env["CODESPY_DATABASE_URL"] = url.render_as_string(hide_password=False)
    env["PYTHONPATH"] = str(BACKEND_DIR)

    image = os.environ.get(
        "CODESPY_SOURCE_READER_IMAGE",
        os.environ.get("CODESPY_MCP_SOURCE_IMAGE", ""),
    )
    if image and (tmp_path / "objects").is_dir():
        env["CODESPY_SOURCE_ROOT"] = str(tmp_path / "objects")
        env["CODESPY_MCP_SOURCE_IMAGE"] = image
        env["CODESPY_MCP_DOCKER_EXECUTABLE"] = os.environ.get(
            "CODESPY_MCP_DOCKER_EXECUTABLE", "/usr/local/bin/docker"
        )
        env["CODESPY_MCP_DOCKER_SOCKET"] = os.environ.get(
            "CODESPY_MCP_DOCKER_SOCKET", "/Users/amanbhaskar/.docker/run/docker.sock"
        )
    else:
        env.pop("CODESPY_SOURCE_ROOT", None)
        env.pop("CODESPY_MCP_SOURCE_IMAGE", None)
        env.pop("CODESPY_MCP_DOCKER_EXECUTABLE", None)
        env.pop("CODESPY_MCP_DOCKER_SOCKET", None)

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.mcp",
        cwd=BACKEND_DIR,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None
    try:
        # 1. Initialize handshake
        proc.stdin.write(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "test-client", "version": "1.0"},
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        await proc.stdin.drain()
        line = await proc.stdout.readline()
        init_res = json.loads(line.decode())
        assert init_res["id"] == 1

        proc.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode()
        )
        await proc.stdin.drain()

        # 2. Tool discovery over pipe
        proc.stdin.write(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/list",
                        "params": {},
                    }
                )
                + "\n"
            ).encode()
        )
        await proc.stdin.drain()
        line_tools = await proc.stdout.readline()
        tools_res = json.loads(line_tools.decode())
        assert tools_res["id"] == 2
        assert len(tools_res["result"]["tools"]) == 13

        # 3. Database search_exact call
        proc.stdin.write(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "search_exact",
                            "arguments": {
                                "repository_id": str(scope.repository_id),
                                "snapshot_id": str(scope.snapshot_id),
                                "query": "a.py",
                            },
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        await proc.stdin.drain()
        line_search = await proc.stdout.readline()
        call_res = json.loads(line_search.decode())
        assert call_res["id"] == 3
        assert not call_res["result"]["isError"]
        assert call_res["result"]["structuredContent"]["chunk_set_id"] == str(receipt.chunk_set_id)

        # 4. Context assembly call
        proc.stdin.write(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "assemble_context",
                            "arguments": {
                                "repository_id": str(scope.repository_id),
                                "snapshot_id": str(scope.snapshot_id),
                                "query": "print",
                            },
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        await proc.stdin.drain()
        line_context = await proc.stdout.readline()
        context_res = json.loads(line_context.decode())
        assert context_res["id"] == 4
        assert not context_res["result"]["isError"]
        assert len(context_res["result"]["structuredContent"]["items"]) >= 1

        # 5. Native source preview (if reader configured)
        if image and file_id:
            proc.stdin.write(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 5,
                            "method": "tools/call",
                            "params": {
                                "name": "get_source_view",
                                "arguments": {
                                    "repository_id": str(scope.repository_id),
                                    "snapshot_id": str(scope.snapshot_id),
                                    "file_id": str(file_id),
                                },
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
            await proc.stdin.drain()
            line_source = await proc.stdout.readline()
            source_res = json.loads(line_source.decode())
            assert source_res["id"] == 5
            assert not source_res["result"]["isError"]
            assert source_res["result"]["structuredContent"]["content_text"] == "print(1)\n"

        # 6. Read saved investigation status and claims
        proc.stdin.write(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 6,
                        "method": "tools/call",
                        "params": {
                            "name": "get_investigation_status",
                            "arguments": {
                                "repository_id": str(scope.repository_id),
                                "investigation_id": str(inv.id),
                            },
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        await proc.stdin.drain()
        line_inv = await proc.stdout.readline()
        inv_res = json.loads(line_inv.decode())
        assert inv_res["id"] == 6
        assert not inv_res["result"]["isError"]
        assert inv_res["result"]["structuredContent"]["investigation_id"] == str(inv.id)

        proc.stdin.write(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {
                            "name": "list_claims",
                            "arguments": {
                                "repository_id": str(scope.repository_id),
                                "investigation_id": str(inv.id),
                            },
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        await proc.stdin.drain()
        line_claims = await proc.stdout.readline()
        claims_res = json.loads(line_claims.decode())
        assert claims_res["id"] == 7
        assert not claims_res["result"]["isError"]
        assert claims_res["result"]["structuredContent"]["claims"] == []

        # 7. Observe proper structured error on grant revocation
        grants = PHASE2_TABLES["repository_grants"]
        async with migration_factory() as s, s.begin():
            await s.execute(
                grants.update()
                .where(
                    grants.c.org_id == ctx.org_id,
                    grants.c.repository_id == scope.repository_id,
                    grants.c.principal_id == ctx.principal_id,
                )
                .values(revoked_at=text("clock_timestamp()"))
            )

        proc.stdin.write(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 8,
                        "method": "tools/call",
                        "params": {
                            "name": "search_exact",
                            "arguments": {
                                "repository_id": str(scope.repository_id),
                                "snapshot_id": str(scope.snapshot_id),
                                "query": "a.py",
                            },
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        await proc.stdin.drain()
        line_revoked = await proc.stdout.readline()
        call_res_revoked = json.loads(line_revoked.decode())
        assert call_res_revoked["id"] == 8
        assert call_res_revoked["result"]["isError"] is True
        assert call_res_revoked["result"]["structuredContent"]["code"] == "scope_denied"
    finally:
        proc.stdin.close()
        await proc.wait()
        err = await proc.stderr.read()
        print("STDERR OUT:", err.decode())
        assert proc.returncode == 0
