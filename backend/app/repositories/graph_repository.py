from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Table, insert, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.contracts.persistence import EdgeCreateV1, InsertSummary, NeighborPage, NodeCreateV1
from app.domain.errors import DuplicateConflict
from app.domain.scope import RunRef, TenantContext
from app.models.tables import cpg_edges, cpg_nodes
from app.repositories.analysis_repository import AnalysisRepository


def scope_filter(table: Table, ref: RunRef) -> tuple[ColumnElement[bool], ...]:
    return (
        table.c.org_id == ref.org_id,
        table.c.snapshot_id == ref.snapshot_id,
        table.c.run_id == ref.run_id,
    )


def node_dto(row: RowMapping) -> NodeCreateV1:
    data = dict(row)
    for n in ["org_id", "snapshot_id", "run_id", "created_at"]:
        data.pop(n)
    data["range"] = {
        n: data.pop(n)
        for n in ["start_byte", "end_byte", "start_line", "end_line", "start_col", "end_col"]
    }
    return NodeCreateV1(**data)


def edge_dto(row: RowMapping) -> EdgeCreateV1:
    return EdgeCreateV1(**{k: row[k] for k in EdgeCreateV1.model_fields})


class GraphRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def insert_batch(
        self, ctx: TenantContext, ref: RunRef, nodes: list[NodeCreateV1], edges: list[EdgeCreateV1]
    ) -> InsertSummary:
        await AnalysisRepository(self.session).get(ctx, ref, lock=True)
        if len(nodes) > 1000 or len(edges) > 2000:
            raise ValueError("graph batch exceeds bound")
        counts = []
        operations: list[tuple[Table, Sequence[Any], str, Callable[[RowMapping], Any]]] = [
            (cpg_nodes, nodes, "local_id", node_dto),
            (cpg_edges, edges, "id", edge_dto),
        ]
        for table, records, key, converter in operations:
            inserted = existing = 0
            for record in records:
                row = (
                    (
                        await self.session.execute(
                            select(table).where(
                                *scope_filter(table, ref), table.c[key] == getattr(record, key)
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row:
                    if converter(row) != record:
                        raise DuplicateConflict("graph identity has different content")
                    existing += 1
                else:
                    data = record.model_dump(exclude={"range"})
                    if isinstance(record, NodeCreateV1):
                        data.update(record.range.model_dump())
                    await self.session.execute(
                        insert(table).values(
                            org_id=ref.org_id,
                            snapshot_id=ref.snapshot_id,
                            run_id=ref.run_id,
                            **data,
                        )
                    )
                    inserted += 1
            counts.extend([inserted, existing])
        return InsertSummary(
            inserted_nodes=counts[0],
            existing_nodes=counts[1],
            inserted_edges=counts[2],
            existing_edges=counts[3],
        )

    async def neighbors(
        self,
        ctx: TenantContext,
        ref: RunRef,
        local_id: UUID,
        direction: str = "forward",
        edge_kind: str | None = None,
        limit: int = 50,
        after: UUID | None = None,
    ) -> NeighborPage:
        await AnalysisRepository(self.session).get(ctx, ref, published=True)
        if not 1 <= limit <= 200 or direction not in {"forward", "reverse"}:
            raise ValueError("invalid traversal bounds")
        col = cpg_edges.c.source_local_id if direction == "forward" else cpg_edges.c.target_local_id
        q = select(cpg_edges).where(*scope_filter(cpg_edges, ref), col == local_id)
        if edge_kind:
            q = q.where(cpg_edges.c.edge_kind == edge_kind)
        if after:
            q = q.where(cpg_edges.c.id > after)
        rows = list(
            (await self.session.execute(q.order_by(cpg_edges.c.id).limit(limit + 1))).mappings()
        )
        return NeighborPage(
            scope=ref,
            edges=[edge_dto(r) for r in rows[:limit]],
            truncated=len(rows) > limit,
            next_cursor=str(rows[limit - 1]["id"]) if len(rows) > limit else None,
        )
