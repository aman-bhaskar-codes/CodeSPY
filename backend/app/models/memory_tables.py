"""SQLAlchemy table definition for evidence-backed repository memories."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

from app.models.phase2_tables import ck, col, created, fk, md, orgid, pk, uq

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
    sa.Column("state", sa.String(24), nullable=False, server_default="needs_review"),
    sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    col("creator_id", "uuid", nullable=True),
    col("reviewer_id", "uuid", nullable=True),
    col("accepted_at", "time", nullable=True),
    col("expires_at", "time", nullable=True),
    col("purge_after", "time", nullable=True),
    col("purged_at", "time", nullable=True),
    col("content_digest", nullable=True),
    created(),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    ),
    pk("org_id", "id"),
    uq("uq_memory_scope", "org_id", "repository_id", "id"),
    fk(
        "fk_mem_scoped_snapshot",
        ["org_id", "repository_id", "snapshot_id"],
        "source_snapshots",
        ["org_id", "repository_id", "id"],
    ),
    ck(
        "ck_memory_state",
        "state IN ('needs_review','candidate','accepted','rejected',"
        "'archived','superseded','expired')",
    ),
    ck("ck_memory_version", "version >= 1"),
    fk("fk_mem_repo", ["org_id", "repository_id"], "repositories", ["org_id", "id"]),
    fk(
        "fk_mem_snapshot",
        ["org_id", "snapshot_id"],
        "source_snapshots",
        ["org_id", "id"],
    ),
)


memory_versions = sa.Table(
    "memory_versions",
    md,
    orgid(),
    col("repository_id", "uuid"),
    col("memory_id", "uuid"),
    sa.Column("version", sa.Integer, nullable=False),
    col("content_digest"),
    sa.Column("payload", JSONB().with_variant(sa.JSON, "sqlite"), nullable=True),
    col("purged_at", "time", nullable=True),
    created(),
    pk("org_id", "memory_id", "version"),
    fk(
        "fk_memory_version_scope",
        ["org_id", "repository_id", "memory_id"],
        "repo_memories",
        ["org_id", "repository_id", "id"],
    ),
)

memory_review_events = sa.Table(
    "memory_review_events",
    md,
    orgid(),
    col("repository_id", "uuid"),
    col("id", "uuid"),
    col("memory_id", "uuid"),
    col("actor_id", "uuid"),
    sa.Column("version", sa.Integer, nullable=False),
    col("action"),
    col("reason"),
    created(),
    pk("org_id", "id"),
    fk(
        "fk_memory_review_scope",
        ["org_id", "repository_id", "memory_id"],
        "repo_memories",
        ["org_id", "repository_id", "id"],
    ),
)

memory_commands = sa.Table(
    "memory_commands",
    md,
    orgid(),
    col("repository_id", "uuid"),
    col("request_key"),
    col("actor_id", "uuid"),
    col("request_digest"),
    col("memory_id", "uuid"),
    sa.Column("version", sa.Integer, nullable=False),
    created(),
    pk("org_id", "repository_id", "request_key"),
    fk(
        "fk_memory_command_scope",
        ["org_id", "repository_id", "memory_id"],
        "repo_memories",
        ["org_id", "repository_id", "id"],
    ),
)

MEMORY_TABLES = (repo_memories, memory_versions, memory_review_events, memory_commands)

context_manifests = sa.Table(
    "context_manifests",
    md,
    orgid(),
    col("repository_id", "uuid"),
    col("snapshot_id", "uuid"),
    col("investigation_id", "uuid"),
    col("logical_call_id", "uuid"),
    col("principal_id", "uuid"),
    col("selection_digest"),
    sa.Column("manifest", JSONB().with_variant(sa.JSON, "sqlite"), nullable=False),
    created(),
    pk("org_id", "investigation_id", "logical_call_id"),
    fk(
        "fk_context_manifest_snapshot",
        ["org_id", "repository_id", "snapshot_id"],
        "source_snapshots",
        ["org_id", "repository_id", "id"],
    ),
    fk(
        "fk_context_manifest_investigation",
        ["org_id", "investigation_id"],
        "investigations",
        ["org_id", "id"],
    ),
)
