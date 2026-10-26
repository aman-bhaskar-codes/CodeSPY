# ruff: noqa: E501 -- keep benchmark SQL readable as a query.
"""Measured selective access on 100k nodes / 300k edges; no latency SLA."""

import asyncio
import json
import os
from pathlib import Path

from app.db.transaction import transaction
from app.repositories.evidence_repository import EvidenceRepository
from sqlalchemy import text

from tests.support.fixtures import batch_for, scenario


async def test_selective_query_plans(factory, engine, db_values):
    runs = []
    for _i in range(10):
        _svc, ctx, snap, run, files = await scenario(factory)
        b = batch_for(run.ref, files)
        async with transaction(factory) as s:
            await EvidenceRepository(s).insert_batch(ctx, run.ref, b.evidence[:1])
        async with engine.begin() as c:
            await c.execute(text("SET LOCAL statement_timeout='120s'"))
            await c.execute(text("SET LOCAL idle_in_transaction_session_timeout='180s'"))
            await c.execute(
                text("""INSERT INTO cpg_nodes(org_id,snapshot_id,run_id,local_id,file_id,node_kind,symbol_name,qualified_name,start_byte,end_byte,start_line,end_line,start_col,end_col)
                SELECT :org,:snapshot,:run,md5('node-'||g)::uuid,:file,'function','fixture','fixture',0,4,1,1,0,4 FROM generate_series(1,10000) g"""),
                {"org": ctx.org_id, "snapshot": snap.id, "run": run.id, "file": files[0]},
            )
            await c.execute(
                text("""INSERT INTO cpg_edges(org_id,snapshot_id,run_id,id,source_local_id,target_local_id,edge_kind,support_evidence_id)
                SELECT :org,:snapshot,:run,md5('edge-'||g||'-'||d)::uuid,md5('node-'||g)::uuid,md5('node-'||((g+d-1)%10000+1))::uuid,'CALLS_CANDIDATE',:support FROM generate_series(1,10000) g CROSS JOIN generate_series(1,3) d"""),
                {
                    "org": ctx.org_id,
                    "snapshot": snap.id,
                    "run": run.id,
                    "support": b.evidence[0].id,
                },
            )
            await c.execute(
                text(
                    "UPDATE analysis_runs SET status='partial',write_version=2,finished_at=CURRENT_TIMESTAMP,result_hash=:hash,coverage=CAST(:coverage AS jsonb) WHERE org_id=:org AND snapshot_id=:snapshot AND id=:run"
                ),
                {
                    "hash": "f" * 64,
                    "coverage": json.dumps(
                        {
                            "status": "partial",
                            "scope_description": "query-plan fixture",
                            "admitted_count": 10000,
                            "processed_count": 10000,
                            "omitted_count": 0,
                            "truncated": False,
                        }
                    ),
                    "org": ctx.org_id,
                    "snapshot": snap.id,
                    "run": run.id,
                },
            )
        runs.append((ctx, snap, run, files, b))
    # ANALYZE is an owner operation; app-role ANALYZE only emits a warning and skips.
    from scripts import phase1_db as db

    def analyze_owned_tables():
        with db.connect(
            db_values,
            db_values["CODESPY_TEST_DATABASE"],
            user="p1_migration",
            password=db_values["CODESPY_TEST_MIGRATION_PASSWORD"],
        ) as admin:
            admin.execute("ANALYZE public.cpg_nodes")
            admin.execute("ANALYZE public.cpg_edges")
            admin.execute("ANALYZE public.evidence_records")

    await asyncio.to_thread(analyze_owned_tables)
    async with engine.begin() as c:
        ctx, snap, run, files, b = runs[0]
        params = {
            "org": ctx.org_id,
            "snapshot": snap.id,
            "run": run.id,
            "file": files[0],
            "support": b.evidence[0].id,
        }
        queries = {
            "forward": "SELECT target_local_id,support_evidence_id FROM cpg_edges WHERE org_id=:org AND snapshot_id=:snapshot AND run_id=:run AND source_local_id=md5('node-1')::uuid AND edge_kind='CALLS_CANDIDATE' LIMIT 50",
            "reverse": "SELECT source_local_id,support_evidence_id FROM cpg_edges WHERE org_id=:org AND snapshot_id=:snapshot AND run_id=:run AND target_local_id=md5('node-1')::uuid AND edge_kind='CALLS_CANDIDATE' LIMIT 50",
            "outline": "SELECT local_id,start_byte FROM cpg_nodes WHERE org_id=:org AND snapshot_id=:snapshot AND run_id=:run AND file_id=:file ORDER BY start_byte,local_id LIMIT 50",
            "evidence": "SELECT id FROM evidence_records WHERE org_id=:org AND snapshot_id=:snapshot AND run_id=:run AND file_id=:file LIMIT 50",
        }
        plans = {}
        for key, q in queries.items():
            rows = (await c.execute(text(q), params)).all()
            assert len(rows) <= 50
            plan = (
                await c.execute(text("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + q), params)
            ).scalar_one()[0]
            plans[key] = plan
            if key != "evidence":
                assert "Index" in json.dumps(plan) or "Bitmap" in json.dumps(plan)
                assert "Seq Scan" not in json.dumps(plan)

                def inspected_rows(node):
                    return node.get("Rows Removed by Filter", 0) + sum(
                        inspected_rows(child) for child in node.get("Plans", [])
                    )

                assert inspected_rows(plan["Plan"]) < 100
        counts = {
            "cpg_nodes": await c.scalar(text("SELECT count(*) FROM cpg_nodes")),
            "cpg_edges": await c.scalar(text("SELECT count(*) FROM cpg_edges")),
        }
        assert counts == {"cpg_nodes": 100000, "cpg_edges": 300000}
    target = Path(os.environ.get("CODESPY_PLAN_OUTPUT", "work/phase1/query-plans.json"))
    target.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(
        target.write_text,
        json.dumps({"dataset": counts, "plans": plans}, indent=2, default=str) + "\n",
    )
