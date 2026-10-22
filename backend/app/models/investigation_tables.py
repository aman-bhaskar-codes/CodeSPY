"""SQLAlchemy tables for investigations, durable checkpoints, and event stream."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from app.models.phase2_tables import ck, col, created, fk, md, orgid, pk

investigations = sa.Table(
    "investigations",
    md,
    orgid(),
    col("id", "uuid"),
    col("repository_id", "uuid"),
    col("principal_id", "uuid"),
    col("finding_id", "uuid", nullable=True),
    sa.Column("target_type", sa.String(32), nullable=False),
    sa.Column("query", sa.Text, nullable=False),
    sa.Column("status", sa.String(32), server_default="pending", nullable=False),
    sa.Column("mode", sa.String(32), server_default="short_path", nullable=False),
    sa.Column("depth", sa.String(32), server_default="standard", nullable=False),
    sa.Column("current_step", sa.Integer, server_default="0", nullable=False),
    sa.Column("max_steps", sa.Integer, server_default="10", nullable=False),
    sa.Column("fencing_token", sa.BigInteger, server_default="1", nullable=False),
    sa.Column("worker_id", sa.String(128), nullable=True),
    sa.Column("worker_lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("budget_limit_micros", sa.BigInteger, nullable=False),
    sa.Column("spent_micros", sa.BigInteger, server_default="0", nullable=False),
    sa.Column("verdict", sa.String(32), nullable=True),
    sa.Column("verdict_confidence", sa.Float, nullable=True),
    sa.Column("summary", sa.Text, nullable=True),
    sa.Column("failure_reason", sa.Text, nullable=True),
    created(),
    sa.Column(
        "updated_at",
        sa.TIMESTAMP(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    pk("org_id", "id"),
    ck(
        "ck_investigation_status",
        "status IN ('pending', 'running', 'checkpointed', 'awaiting_human_decision', "
        "'completed', 'failed', 'cancelled')",
    ),
    ck(
        "ck_investigation_target_type",
        "target_type IN ('finding', 'repository', 'commit', 'diff')",
    ),
    ck("ck_investigation_mode", "mode IN ('short_path', 'two_specialist')"),
    ck("ck_investigation_depth", "depth IN ('quick', 'standard', 'deep')"),
    ck(
        "ck_investigation_verdict",
        "verdict IS NULL OR verdict IN ('true_positive', 'false_positive', "
        "'indeterminate', 'benign')",
    ),
    ck("ck_investigation_budget_pos", "budget_limit_micros > 0"),
    ck("ck_investigation_spent_nonneg", "spent_micros >= 0"),
    ck(
        "ck_investigation_steps",
        "current_step >= 0 AND max_steps > 0 AND current_step <= max_steps + 1",
    ),
    ck("ck_investigation_fencing_pos", "fencing_token >= 1"),
    fk("fk_investigation_repo", ["org_id", "repository_id"], "repositories", ["org_id", "id"]),
    fk(
        "fk_investigation_member",
        ["org_id", "principal_id"],
        "memberships",
        ["org_id", "principal_id"],
    ),
)

investigation_checkpoints = sa.Table(
    "investigation_checkpoints",
    md,
    orgid(),
    col("investigation_id", "uuid"),
    sa.Column("step_index", sa.Integer, nullable=False),
    sa.Column("fencing_token", sa.BigInteger, nullable=False),
    sa.Column("worker_id", sa.String(128), nullable=False),
    sa.Column("graph_node", sa.String(64), nullable=False),
    sa.Column("state_snapshot_json", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
    created(),
    pk("org_id", "investigation_id", "step_index"),
    ck("ck_checkpoint_step_nonneg", "step_index >= 0"),
    ck("ck_checkpoint_fencing_pos", "fencing_token >= 1"),
    fk(
        "fk_checkpoint_investigation",
        ["org_id", "investigation_id"],
        "investigations",
        ["org_id", "id"],
    ),
)

investigation_events = sa.Table(
    "investigation_events",
    md,
    orgid(),
    col("id", "uuid"),
    col("investigation_id", "uuid"),
    sa.Column("step_index", sa.Integer, nullable=False),
    sa.Column("event_type", sa.String(64), nullable=False),
    sa.Column("node_name", sa.String(64), nullable=False),
    sa.Column("payload_json", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
    created(),
    pk("org_id", "id"),
    ck("ck_event_step_nonneg", "step_index >= 0"),
    fk(
        "fk_event_investigation",
        ["org_id", "investigation_id"],
        "investigations",
        ["org_id", "id"],
    ),
)
