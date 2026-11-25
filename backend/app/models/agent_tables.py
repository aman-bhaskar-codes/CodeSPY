"""SQLAlchemy Core tables for agent sessions and their append-only event log (AEP/1)."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from app.models.phase2_tables import ck, col, created, fk, md, orgid, pk, uq

_JSON = JSONB().with_variant(sa.JSON(), "sqlite")

agent_sessions = sa.Table(
    "agent_sessions",
    md,
    orgid(),
    col("id", "uuid"),
    col("repository_id", "uuid"),
    col("base_snapshot_id", "uuid"),
    col("principal_id", "uuid"),
    col("mode"),
    col("status", default="'idle'"),
    col("model_profile"),
    sa.Column("limits_json", _JSON, nullable=False),
    col("last_seq", "bigint", default="0"),
    col("idempotency_key", nullable=True),
    col("request_digest", nullable=True),
    col("lease_owner", nullable=True),
    col("lease_until", "time", nullable=True),
    col("control", nullable=True),
    created(),
    col("updated_at", "time", default="clock_timestamp()"),
    pk("org_id", "id"),
    uq("uq_agent_session_idempotency", "org_id", "principal_id", "idempotency_key"),
    ck("ck_agent_session_mode", "mode IN ('plan','act')"),
    ck(
        "ck_agent_session_status",
        "status IN ('idle','running','waiting_approval','interrupted',"
        "'completed','failed','cancelled')",
    ),
    ck("ck_agent_session_profile", "model_profile IN ('agent','fast','deep')"),
)

agent_events = sa.Table(
    "agent_events",
    md,
    orgid(),
    col("session_id", "uuid"),
    col("seq", "bigint"),
    col("type"),
    col("turn", "int", default="0"),
    col("parent", nullable=True),
    col("event_key", nullable=True),
    col("payload_ciphertext", "bytes"),
    col("payload_bytes", "int"),
    created(),
    pk("org_id", "session_id", "seq"),
    fk("fk_agent_event_session", ("org_id", "session_id"), "agent_sessions", ("org_id", "id")),
    uq("uq_agent_event_key", "org_id", "session_id", "event_key"),
)

agent_session_checkpoints = sa.Table(
    "agent_session_checkpoints",
    md,
    orgid(),
    col("session_id", "uuid"),
    col("sequence", "int"),
    col("state_digest"),
    col("state_ciphertext", "bytes"),
    created(),
    pk("org_id", "session_id", "sequence"),
    fk("fk_agent_checkpoint_session", ("org_id", "session_id"), "agent_sessions", ("org_id", "id")),
)

agent_approvals = sa.Table(
    "agent_approvals",
    md,
    orgid(),
    col("session_id", "uuid"),
    col("approval_id"),
    col("subject_kind"),
    col("subject_ref"),
    col("status", default="'pending'"),
    col("reason", default="''"),
    sa.Column("rule_json", _JSON, nullable=True),
    col("requested_seq", "bigint"),
    col("decided_by", "uuid", nullable=True),
    col("decided_at", "time", nullable=True),
    col("expires_at", "time"),
    created(),
    pk("org_id", "session_id", "approval_id"),
    fk("fk_agent_approval_session", ("org_id", "session_id"), "agent_sessions", ("org_id", "id")),
)

agent_effects = sa.Table(
    "agent_effects",
    md,
    orgid(),
    col("session_id", "uuid"),
    col("effect_id"),
    col("phase"),
    col("kind"),
    col("payload_ciphertext", "bytes"),
    col("payload_bytes", "int"),
    created(),
    pk("org_id", "session_id", "effect_id", "phase"),
    fk("fk_agent_effect_session", ("org_id", "session_id"), "agent_sessions", ("org_id", "id")),
)
