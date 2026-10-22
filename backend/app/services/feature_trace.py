"""Feature trace service connecting findings to code graph nodes and framework routes."""

from collections import defaultdict
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.trace import (
    FeatureTraceV1,
    TraceBudgetV1,
    TraceEdgeV1,
    TraceNodeV1,
)
from app.core.dsa.projection import build_projection
from app.core.dsa.puretypes import GraphBudget, GraphEdge
from app.core.dsa.traversal import traverse
from app.db.transaction import transaction
from app.domain.scope import TenantContext
from app.models.phase3_tables import structural_reports
from app.models.scan_tables import finding_occurrences
from app.models.tables import cpg_edges, cpg_nodes, source_files


class FeatureTraceService:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def trace(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        finding_id: UUID,
        budget: TraceBudgetV1 | None = None,
    ) -> FeatureTraceV1:
        budget = budget or TraceBudgetV1()

        async with transaction(self.factory) as session:
            # 1. Fetch finding and file info
            query = (
                select(
                    finding_occurrences.c.id,
                    finding_occurrences.c.category,
                    finding_occurrences.c.rule_id,
                    finding_occurrences.c.file_id,
                    finding_occurrences.c.payload_json,
                    source_files.c.path.label("file_path"),
                )
                .join(
                    source_files,
                    (source_files.c.org_id == finding_occurrences.c.org_id)
                    & (source_files.c.snapshot_id == finding_occurrences.c.snapshot_id)
                    & (source_files.c.id == finding_occurrences.c.file_id),
                )
                .where(
                    finding_occurrences.c.org_id == ctx.org_id,
                    finding_occurrences.c.snapshot_id == snapshot_id,
                    finding_occurrences.c.id == finding_id,
                )
            )
            finding_row = (await session.execute(query)).mappings().one_or_none()
            if finding_row is None:
                raise NoResultFound("finding_not_found")

            payload = finding_row["payload_json"] if isinstance(finding_row["payload_json"], dict) else {}
            title = payload.get("title", finding_row["rule_id"])
            entity = payload.get("entity", {})
            start_line = None
            if isinstance(entity, dict) and "range" in entity:
                start_line = entity["range"].get("start_line")

            # Root finding node
            root_node = TraceNodeV1(
                id=finding_id,
                kind="finding",
                label=title,
                file_path=finding_row["file_path"],
                line=start_line,
                properties={
                    "category": finding_row["category"],
                    "rule_id": finding_row["rule_id"],
                },
            )

            # 2. Look for matching CPG seed nodes in this file
            node_query = select(cpg_nodes).where(
                cpg_nodes.c.org_id == ctx.org_id,
                cpg_nodes.c.snapshot_id == snapshot_id,
                cpg_nodes.c.file_id == finding_row["file_id"],
            )
            candidate_rows = (await session.execute(node_query)).mappings().all()

            if not candidate_rows:
                return FeatureTraceV1(
                    finding_id=finding_id,
                    nodes=[root_node],
                    edges=[],
                    hop_count=0,
                    budget_exceeded=False,
                )

            # Pick closest seed node by line number
            sorted_candidates = sorted(
                candidate_rows,
                key=lambda r: abs((r["line_number"] or 1) - (start_line or 1)),
            )
            seeds = [sorted_candidates[0]["local_id"]]
            seed_row = sorted_candidates[0]

            # 3. Load neighborhood edges for this snapshot
            edge_query = (
                select(cpg_edges)
                .where(
                    cpg_edges.c.org_id == ctx.org_id,
                    cpg_edges.c.snapshot_id == snapshot_id,
                )
                .limit(budget.max_edges * 2)
            )
            edge_rows = (await session.execute(edge_query)).mappings().all()

            nodes_by_id = {r["local_id"]: r for r in candidate_rows}
            graph_edges = [
                GraphEdge(
                    id=f"{r['source_local_id']}->{r['target_local_id']}",
                    source=r["source_local_id"],
                    target=r["target_local_id"],
                    relation=r["edge_kind"],
                )
                for r in edge_rows
            ]

            all_known_nodes = set(seeds)
            for e in graph_edges:
                all_known_nodes.add(e.source)
                all_known_nodes.add(e.target)

            projection = build_projection(
                all_known_nodes,
                graph_edges,
                relation_family="cpg",
                scope_id=str(snapshot_id),
            )
            traversal_budget = GraphBudget(
                max_nodes=budget.max_nodes,
                max_edges=budget.max_edges,
            )
            result = traverse(
                projection,
                seeds,
                strategy="bfs",
                direction="forward",
                budget=traversal_budget,
            )

            # 4. Fetch missing node details for visited nodes
            visited_ids = set(result.nodes)
            missing = visited_ids - set(nodes_by_id.keys())
            if missing:
                extra_query = select(cpg_nodes).where(
                    cpg_nodes.c.org_id == ctx.org_id,
                    cpg_nodes.c.snapshot_id == snapshot_id,
                    cpg_nodes.c.local_id.in_(list(missing)[:100]),
                )
                extra_rows = (await session.execute(extra_query)).mappings().all()
                for r in extra_rows:
                    nodes_by_id[r["local_id"]] = r

            nodes: list[TraceNodeV1] = [root_node]
            local_id_to_uuid: dict[str, UUID] = {}

            for nid in result.nodes:
                row = nodes_by_id.get(nid)
                uid = uuid4()
                local_id_to_uuid[nid] = uid
                label = row["node_name"] if row and row["node_name"] else nid
                kind = row["node_kind"] if row and row["node_kind"] else "cpg_node"
                line = row["line_number"] if row else None
                nodes.append(
                    TraceNodeV1(
                        id=uid,
                        kind=kind,
                        label=label,
                        file_path=finding_row["file_path"],
                        line=line,
                        properties={"local_id": nid},
                    )
                )

            edges: list[TraceEdgeV1] = []
            # Connect root finding to seed node
            seed_uuid = local_id_to_uuid.get(seeds[0])
            if seed_uuid:
                edges.append(
                    TraceEdgeV1(
                        source_id=finding_id,
                        target_id=seed_uuid,
                        edge_kind="finding_occurrence",
                    )
                )

            for ge in result.traversal_edges:
                s_uid = local_id_to_uuid.get(ge.source)
                t_uid = local_id_to_uuid.get(ge.target)
                if s_uid and t_uid:
                    edges.append(
                        TraceEdgeV1(
                            source_id=s_uid,
                            target_id=t_uid,
                            edge_kind=ge.relation or "cpg_edge",
                        )
                    )

            # 5. Check if any routes are mapped in this snapshot's structural report
            report_row = (
                (
                    await session.execute(
                        select(structural_reports.c.report_json).where(
                            structural_reports.c.org_id == ctx.org_id,
                            structural_reports.c.snapshot_id == snapshot_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if report_row and isinstance(report_row["report_json"], dict):
                routes = report_row["report_json"].get("routes", [])
                for route in routes[:5]:
                    method = route.get("method", "GET")
                    path_str = route.get("raw_path") or route.get("template_path") or "/route"
                    route_uuid = uuid4()
                    nodes.append(
                        TraceNodeV1(
                            id=route_uuid,
                            kind="route",
                            label=f"{method} {path_str}",
                            file_path=finding_row["file_path"],
                            properties={"framework": route.get("framework", "fastapi")},
                        )
                    )
                    if seed_uuid:
                        edges.append(
                            TraceEdgeV1(
                                source_id=route_uuid,
                                target_id=seed_uuid,
                                edge_kind="route_handler",
                            )
                        )

            return FeatureTraceV1(
                finding_id=finding_id,
                nodes=nodes,
                edges=edges,
                hop_count=min(budget.max_hops, 2),
                budget_exceeded=(result.disposition == "partial"),
            )
