"""Safety-filtered recent-memory baseline for the frozen human-note corpus only."""

from app.contracts.memory import MemoryEntryV1
from app.models.memory_tables import repo_memories
from app.security.authorization import require_repository
from app.services.analysis_sources import lock_retained_snapshot
from sqlalchemy import func, select


async def recent_notes(session, actor, repo, snapshot):
    await require_repository(session, actor.org_id, actor.principal_id, repo)
    rows = (
        (
            await session.execute(
                select(repo_memories)
                .where(
                    repo_memories.c.org_id == actor.org_id,
                    repo_memories.c.repository_id == repo,
                    repo_memories.c.state == "accepted",
                    repo_memories.c.expires_at > func.clock_timestamp(),
                    repo_memories.c.purged_at.is_(None),
                )
                .order_by(repo_memories.c.accepted_at.desc(), repo_memories.c.id)
                .limit(100)
            )
        )
        .mappings()
        .all()
    )
    chosen = []
    historical = 0
    checked = set()
    for row in rows:
        # This baseline does not pretend to qualify arbitrary source-backed memories.
        assert row["evidence_pointers"] == []
        history = row["snapshot_id"] != snapshot
        if history and historical == 2:
            continue
        if row["snapshot_id"] not in checked:
            await lock_retained_snapshot(session, actor.org_id, repo, row["snapshot_id"])
            checked.add(row["snapshot_id"])
        values = {key: row[key] for key in MemoryEntryV1.model_fields if key in row}
        values.update(
            memory_id=row["id"], historical_hint=history, source_correspondence="human_note"
        )
        chosen.append(MemoryEntryV1.model_validate(values))
        historical += int(history)
        if len(chosen) == 5:
            break
    return tuple(chosen)
