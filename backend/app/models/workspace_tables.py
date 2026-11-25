"""SQLAlchemy Core tables for agent workspaces and their append-only revision log."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from app.models.phase2_tables import col, created, fk, md, orgid, pk, uq

_JSON = JSONB().with_variant(sa.JSON(), "sqlite")

workspaces = sa.Table(
    "workspaces",
    md,
    orgid(),
    col("id", "uuid"),
    col("session_id", "uuid"),
    col("repository_id", "uuid"),
    col("base_snapshot_id", "uuid"),
    col("head_revision", "bigint", default="0"),
    col("sandbox_ref", nullable=True),
    col("status", default="'open'"),
    created(),
    col("updated_at", "time", default="clock_timestamp()"),
    pk("org_id", "id"),
    uq("uq_workspace_session", "org_id", "session_id"),
)

workspace_revisions = sa.Table(
    "workspace_revisions",
    md,
    orgid(),
    col("workspace_id", "uuid"),
    col("revision", "bigint"),
    col("author"),
    col("change_id"),
    col("request_digest", nullable=True),
    sa.Column("changes_json", _JSON, nullable=False),
    col("tree_hash"),
    col("sandbox_revision", "bigint", nullable=True),
    col("recovered", "bool", default="false"),
    created(),
    pk("org_id", "workspace_id", "revision"),
    fk(
        "fk_workspace_revision_workspace",
        ("org_id", "workspace_id"),
        "workspaces",
        ("org_id", "id"),
    ),
)

workspace_checkpoints = sa.Table(
    "workspace_checkpoints",
    md,
    orgid(),
    col("id", "uuid"),
    col("workspace_id", "uuid"),
    col("revision", "bigint"),
    col("tree_hash"),
    col("label", default="''"),
    col("sandbox_checkpoint_id", "uuid", nullable=True),
    col("bundle_ref", nullable=True),
    created(),
    pk("org_id", "id"),
    fk(
        "fk_workspace_checkpoint_workspace",
        ("org_id", "workspace_id"),
        "workspaces",
        ("org_id", "id"),
    ),
)
