"""Bounded, source-linked neighborhoods from one published structural run."""

import asyncio
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.trace import FeatureTraceV1, TraceBudgetV1, TraceEdgeV1, TraceNodeV1
from app.core.dsa.projection import build_projection
from app.core.dsa.puretypes import GraphBudget, GraphEdge
from app.core.dsa.traversal import traverse
from app.db.transaction import transaction
from app.domain.errors import CapacityExceeded
from app.domain.scope import TenantContext
from app.models.phase3_tables import structural_reports
from app.models.scan_tables import finding_occurrences
from app.models.tables import analysis_runs, cpg_edges, cpg_nodes, source_files
from app.security.authorization import require_snapshot


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
        try:
            async with asyncio.timeout(budget.timeout_seconds):
                return await self._trace(ctx, repository_id, snapshot_id, finding_id, budget)
        except TimeoutError:
            raise CapacityExceeded("feature_trace_timeout") from None

    async def _trace(
        self,
        ctx: TenantContext,
        repository_id: UUID,
        snapshot_id: UUID,
        finding_id: UUID,
        budget: TraceBudgetV1,
    ) -> FeatureTraceV1:
        async with transaction(self.factory) as session:
            await require_snapshot(session, ctx, repository_id, snapshot_id)
            row = (
                (
                    await session.execute(
                        select(finding_occurrences, source_files.c.path.label("file_path"))
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
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NoResultFound("finding_not_found")
            payload = row["payload_json"] if isinstance(row["payload_json"], dict) else {}
            entity = payload.get("entity", {})
            location = entity.get("range", {}) if isinstance(entity, dict) else {}
            location = location if isinstance(location, dict) else {}
            line = location.get("start_line")
            line = line if isinstance(line, int) and line > 0 else None
            root = TraceNodeV1(
                id=finding_id,
                kind="finding",
                label=str(payload.get("title", row["rule_id"]))[:256],
                file_path=row["file_path"],
                line=line,
                properties={"category": row["category"], "rule_id": row["rule_id"]},
            )
            run_id = await session.scalar(
                select(structural_reports.c.run_id)
                .join(
                    analysis_runs,
                    (analysis_runs.c.org_id == structural_reports.c.org_id)
                    & (analysis_runs.c.snapshot_id == structural_reports.c.snapshot_id)
                    & (analysis_runs.c.id == structural_reports.c.run_id),
                )
                .where(
                    structural_reports.c.org_id == ctx.org_id,
                    structural_reports.c.repository_id == repository_id,
                    structural_reports.c.snapshot_id == snapshot_id,
                    analysis_runs.c.status.in_(("completed", "partial")),
                )
                .order_by(
                    structural_reports.c.created_at.desc(), structural_reports.c.run_id.desc()
                )
                .limit(1)
            )

            def root_only(reason: str, *, exceeded: bool = False) -> FeatureTraceV1:
                return FeatureTraceV1(
                    finding_id=finding_id,
                    structural_run_id=run_id,
                    nodes=[root],
                    budget_exceeded=exceeded,
                    omission_reasons=[reason],
                )

            if run_id is None:
                return root_only("structural_report_unavailable")
            if line is None:
                return root_only("finding_location_unavailable")
            node_scope = (
                cpg_nodes.c.org_id == ctx.org_id,
                cpg_nodes.c.snapshot_id == snapshot_id,
                cpg_nodes.c.run_id == run_id,
            )
            containment = [cpg_nodes.c.start_line <= line, cpg_nodes.c.end_line >= line]
            start, end = location.get("start_byte"), location.get("end_byte")
            if isinstance(start, int) and isinstance(end, int) and 0 <= start < end:
                containment.extend((cpg_nodes.c.start_byte <= start, cpg_nodes.c.end_byte >= end))
            seed = await session.scalar(
                select(cpg_nodes.c.local_id)
                .where(*node_scope, cpg_nodes.c.file_id == row["file_id"], *containment)
                .order_by(
                    (cpg_nodes.c.end_byte - cpg_nodes.c.start_byte).asc(),
                    cpg_nodes.c.start_byte.asc(),
                    cpg_nodes.c.local_id.asc(),
                )
                .limit(1)
            )
            if seed is None:
                return root_only("containing_symbol_unavailable")
            if budget.max_nodes == 1:
                return root_only("nodes", exceeded=True)
            if budget.max_edges == 0:
                return root_only("edges", exceeded=True)

            # Read only adjacency reached from this seed; never an arbitrary snapshot edge window.
            seen, frontier = {seed}, {seed}
            edge_rows: list[dict[str, Any]] = []
            reasons: set[str] = set()
            for depth in range(budget.max_hops + 1):
                if not frontier:
                    break
                remaining = budget.max_edges - 1 - len(edge_rows)
                query = (
                    select(cpg_edges)
                    .where(
                        cpg_edges.c.org_id == ctx.org_id,
                        cpg_edges.c.snapshot_id == snapshot_id,
                        cpg_edges.c.run_id == run_id,
                        cpg_edges.c.source_local_id.in_(frontier),
                    )
                    .order_by(
                        cpg_edges.c.source_local_id,
                        cpg_edges.c.target_local_id,
                        cpg_edges.c.edge_kind,
                        cpg_edges.c.id,
                    )
                )
                if depth == budget.max_hops or remaining == 0:
                    if (await session.execute(query.limit(1))).first() is not None:
                        reasons.add("depth" if depth == budget.max_hops else "edges")
                    break
                rows = (await session.execute(query.limit(remaining + 1))).mappings().all()
                if len(rows) > remaining:
                    reasons.add("edges")
                next_frontier: set[UUID] = set()
                for edge in rows[:remaining]:
                    target = edge["target_local_id"]
                    if target not in seen:
                        if len(seen) >= budget.max_nodes - 1:
                            reasons.add("nodes")
                            continue
                        seen.add(target)
                        next_frontier.add(target)
                    edge_rows.append(dict(edge))
                frontier = next_frontier

            node_rows = (
                (
                    await session.execute(
                        select(cpg_nodes, source_files.c.path.label("file_path"))
                        .join(
                            source_files,
                            (source_files.c.org_id == cpg_nodes.c.org_id)
                            & (source_files.c.snapshot_id == cpg_nodes.c.snapshot_id)
                            & (source_files.c.id == cpg_nodes.c.file_id),
                        )
                        .where(*node_scope, cpg_nodes.c.local_id.in_(seen))
                    )
                )
                .mappings()
                .all()
            )
            node_map = {str(n["local_id"]): n for n in node_rows}
            graph_edges = [
                GraphEdge(
                    id=str(e["id"]),
                    source=str(e["source_local_id"]),
                    target=str(e["target_local_id"]),
                    relation=e["edge_kind"],
                    support=(str(e["support_evidence_id"]),),
                )
                for e in edge_rows
                if str(e["source_local_id"]) in node_map and str(e["target_local_id"]) in node_map
            ]
            if str(seed) not in node_map:
                return root_only("containing_symbol_unavailable")
            if len(node_map) != len(seen):
                reasons.add("node_unavailable")
            result = traverse(
                build_projection(
                    node_map, graph_edges, relation_family="cpg", scope_id=f"{snapshot_id}:{run_id}"
                ),
                [str(seed)],
                budget=GraphBudget(
                    max_nodes=budget.max_nodes - 1,
                    max_edges=budget.max_edges - 1,
                    max_depth=budget.max_hops,
                    max_frontier=budget.max_nodes,
                    max_scanned_edges=budget.max_edges,
                    wall_seconds=budget.timeout_seconds,
                ),
            )
            if result.reason:
                reasons.add(result.reason)
            identities = {
                key: uuid5(NAMESPACE_URL, f"codespy:{ctx.org_id}:{snapshot_id}:{run_id}:{key}")
                for key in result.nodes
            }
            nodes = [root] + [
                TraceNodeV1(
                    id=identities[key],
                    kind=node_map[key]["node_kind"],
                    label=node_map[key]["symbol_name"][:256],
                    file_path=node_map[key]["file_path"],
                    line=node_map[key]["start_line"],
                    properties={"local_id": key},
                )
                for key in result.nodes
            ]
            edges = [
                TraceEdgeV1(
                    source_id=finding_id,
                    target_id=identities[str(seed)],
                    edge_kind="finding_occurrence",
                )
            ]
            distances = {str(seed): 0}
            for graph_edge in result.traversal_edges:
                edges.append(
                    TraceEdgeV1(
                        source_id=identities[graph_edge.source],
                        target_id=identities[graph_edge.target],
                        edge_kind=graph_edge.relation,
                        properties={"support_evidence_id": graph_edge.support[0]},
                    )
                )
                if graph_edge.target not in distances:
                    distances[graph_edge.target] = distances[graph_edge.source] + 1
            return FeatureTraceV1(
                finding_id=finding_id,
                structural_run_id=run_id,
                nodes=nodes,
                edges=edges,
                hop_count=max(distances.values()),
                budget_exceeded=bool(reasons),
                omission_reasons=sorted(reasons),
            )
