"""Tenant-scoped reads of published AST facts (latest completed run per snapshot)."""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.ast_facts import (
    AstFilePageV1,
    AstFileSummaryV1,
    AstModelItemV1,
    AstModelPageV1,
    AstRouteItemV1,
    AstRoutePageV1,
    AstSnapshotSummaryV1,
    FileFactsV1,
)
from app.domain.canonical_json import canonical_bytes
from app.models.phase3_tables import ast_file_facts
from app.models.tables import analysis_runs, source_files
from app.security.authorization import require_repository


async def _latest_run(s: AsyncSession, org_id: UUID, repository_id: UUID, snapshot_id: UUID) -> Any:
    return await s.scalar(
        select(ast_file_facts.c.run_id)
        .join(
            analysis_runs,
            (analysis_runs.c.org_id == ast_file_facts.c.org_id)
            & (analysis_runs.c.snapshot_id == ast_file_facts.c.snapshot_id)
            & (analysis_runs.c.id == ast_file_facts.c.run_id),
        )
        .where(
            ast_file_facts.c.org_id == org_id,
            ast_file_facts.c.repository_id == repository_id,
            ast_file_facts.c.snapshot_id == snapshot_id,
            analysis_runs.c.status.in_(["completed", "partial"]),
        )
        .order_by(ast_file_facts.c.created_at.desc(), ast_file_facts.c.run_id.desc())
        .limit(1)
    )


async def summary(
    s: AsyncSession, ctx_org: UUID, principal: UUID, repository_id: UUID, snapshot_id: UUID
) -> AstSnapshotSummaryV1 | None:
    await require_repository(s, ctx_org, principal, repository_id)
    run_id = await _latest_run(s, ctx_org, repository_id, snapshot_id)
    if run_id is None:
        return None
    t = ast_file_facts
    totals = (
        await s.execute(
            select(
                t.c.language,
                func.count(),
                func.sum(t.c.symbol_count),
                func.sum(t.c.import_count),
                func.sum(t.c.export_count),
            )
            .where(t.c.org_id == ctx_org, t.c.snapshot_id == snapshot_id, t.c.run_id == run_id)
            .group_by(t.c.language)
        )
    ).all()
    return AstSnapshotSummaryV1(
        run_id=run_id,
        snapshot_id=snapshot_id,
        files=sum(r[1] for r in totals),
        languages={r[0]: r[1] for r in totals},
        symbols=sum(r[2] for r in totals),
        imports=sum(r[3] for r in totals),
        exports=sum(r[4] for r in totals),
    )


async def files_page(
    s: AsyncSession,
    org_id: UUID,
    principal: UUID,
    repository_id: UUID,
    snapshot_id: UUID,
    after: str | None,
    limit: int,
) -> AstFilePageV1 | None:
    await require_repository(s, org_id, principal, repository_id)
    run_id = await _latest_run(s, org_id, repository_id, snapshot_id)
    if run_id is None:
        return None
    t = ast_file_facts
    query = (
        select(
            t.c.file_id,
            source_files.c.path,
            t.c.language,
            t.c.status,
            t.c.facts_sha256,
            t.c.symbol_count,
            t.c.import_count,
            t.c.export_count,
        )
        .join(
            source_files,
            (source_files.c.org_id == t.c.org_id)
            & (source_files.c.snapshot_id == t.c.snapshot_id)
            & (source_files.c.id == t.c.file_id),
        )
        .where(t.c.org_id == org_id, t.c.snapshot_id == snapshot_id, t.c.run_id == run_id)
        .order_by(source_files.c.path)
        .limit(limit + 1)
    )
    if after:
        query = query.where(source_files.c.path > after)
    rows = (await s.execute(query)).all()
    page = rows[:limit]
    return AstFilePageV1(
        run_id=run_id,
        items=tuple(
            AstFileSummaryV1(
                file_id=r[0],
                path=r[1],
                language=r[2],
                status=r[3],
                facts_sha256=r[4],
                symbols=r[5],
                imports=r[6],
                exports=r[7],
            )
            for r in page
        ),
        next_after=page[-1][1] if len(rows) > limit else None,
    )


async def file_facts(
    s: AsyncSession,
    org_id: UUID,
    principal: UUID,
    repository_id: UUID,
    snapshot_id: UUID,
    file_id: UUID,
) -> FileFactsV1 | None:
    await require_repository(s, org_id, principal, repository_id)
    run_id = await _latest_run(s, org_id, repository_id, snapshot_id)
    if run_id is None:
        return None
    t = ast_file_facts
    value = await s.scalar(
        select(t.c.facts_json).where(
            t.c.org_id == org_id,
            t.c.snapshot_id == snapshot_id,
            t.c.run_id == run_id,
            t.c.file_id == file_id,
        )
    )
    return None if value is None else FileFactsV1.model_validate_json(canonical_bytes(value))


def _cursor(after: str | None) -> tuple[str, int]:
    """Cursor 'ordinal:file path' marks the last row returned."""
    if not after:
        return "", 0
    ordinal, _, path = after.partition(":")
    return path, int(ordinal) if ordinal.isdigit() else 0


_ARRAY_QUERY = """
SELECT f.file_id, sf.path, e.ord, e.value
FROM ast_file_facts f
JOIN source_files sf
  ON sf.org_id=f.org_id AND sf.snapshot_id=f.snapshot_id AND sf.id=f.file_id
CROSS JOIN LATERAL jsonb_array_elements(f.facts_json->:key) WITH ORDINALITY AS e(value, ord)
WHERE f.org_id=:org AND f.snapshot_id=:snapshot AND f.run_id=:run
  AND (sf.path, e.ord) > (:path, :ord)
ORDER BY sf.path, e.ord
LIMIT :limit
"""


async def _array_rows(
    s: AsyncSession,
    key: str,
    org_id: UUID,
    snapshot_id: UUID,
    run_id: UUID,
    after: str | None,
    limit: int,
) -> list[Any]:
    path, ordinal = _cursor(after)
    result = await s.execute(
        text(_ARRAY_QUERY),
        {
            "key": key,
            "org": org_id,
            "snapshot": snapshot_id,
            "run": run_id,
            "path": path,
            "ord": ordinal,
            "limit": limit + 1,
        },
    )
    return list(result.all())


def _next(rows: list[Any], limit: int) -> str | None:
    return f"{rows[limit - 1][2]}:{rows[limit - 1][1]}" if len(rows) > limit else None


async def routes_page(
    s: AsyncSession,
    org_id: UUID,
    principal: UUID,
    repository_id: UUID,
    snapshot_id: UUID,
    after: str | None,
    limit: int,
) -> AstRoutePageV1 | None:
    await require_repository(s, org_id, principal, repository_id)
    run_id = await _latest_run(s, org_id, repository_id, snapshot_id)
    if run_id is None:
        return None
    rows = await _array_rows(s, "routes", org_id, snapshot_id, run_id, after, limit)
    return AstRoutePageV1(
        run_id=run_id,
        items=tuple(
            AstRouteItemV1(
                file_id=r[0],
                file_path=r[1],
                framework=r[3]["framework"],
                method=r[3]["method"],
                route_path=r[3]["path"],
                handler=r[3].get("handler"),
                start_line=r[3]["range"]["start_line"],
                end_line=r[3]["range"]["end_line"],
            )
            for r in rows[:limit]
        ),
        next_after=_next(rows, limit),
    )


async def models_page(
    s: AsyncSession,
    org_id: UUID,
    principal: UUID,
    repository_id: UUID,
    snapshot_id: UUID,
    after: str | None,
    limit: int,
) -> AstModelPageV1 | None:
    await require_repository(s, org_id, principal, repository_id)
    run_id = await _latest_run(s, org_id, repository_id, snapshot_id)
    if run_id is None:
        return None
    rows = await _array_rows(s, "models", org_id, snapshot_id, run_id, after, limit)
    return AstModelPageV1(
        run_id=run_id,
        items=tuple(
            AstModelItemV1.model_validate_json(
                canonical_bytes(
                    {
                        "file_id": str(r[0]),
                        "file_path": r[1],
                        "framework": r[3]["framework"],
                        "name": r[3]["name"],
                        "table": r[3].get("table"),
                        "columns": r[3].get("columns", []),
                        "start_line": r[3]["range"]["start_line"],
                        "end_line": r[3]["range"]["end_line"],
                    }
                )
            )
            for r in rows[:limit]
        ),
        next_after=_next(rows, limit),
    )
