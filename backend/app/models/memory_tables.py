"""SQLAlchemy table definition for evidence-backed repository memories."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

from app.models.phase2_tables import col, created, fk, md, orgid, pk

repo_memories = sa.Table(
    "repo_memories",
    md,
    orgid(),
    col("id", "uuid"),
    col("repository_id", "uuid"),
    col("snapshot_id", "uuid"),
    sa.Column("memory_kind", sa.String(32), nullable=False),
    sa.Column("topic", sa.String(128), nullable=False),
    sa.Column("summary", sa.Text, nullable=False),
    sa.Column(
        "evidence_pointers",
        JSONB().with_variant(sa.JSON, "sqlite"),
        nullable=False,
        server_default="[]",
    ),
    sa.Column("tsv", TSVECTOR().with_variant(sa.Text, "sqlite"), nullable=True),
    created(),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    ),
    pk("org_id", "id"),
    fk("fk_mem_repo", ["org_id", "repository_id"], "repositories", ["org_id", "id"]),
    fk(
        "fk_mem_snapshot",
        ["org_id", "snapshot_id"],
        "source_snapshots",
        ["org_id", "id"],
    ),
)
