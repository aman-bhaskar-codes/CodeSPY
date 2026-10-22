"""SQLAlchemy tables for model gateway capabilities, generation reservations, and spend ledger."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from app.models.phase2_tables import ck, col, created, fk, md, orgid, pk, uq

model_capabilities = sa.Table(
    "model_capabilities",
    md,
    sa.Column("provider", sa.String(64), nullable=False),
    sa.Column("model_id", sa.String(128), nullable=False),
    sa.Column("revision", sa.String(32), nullable=False),
    sa.Column("capability_type", sa.String(32), nullable=False),
    sa.Column("max_context_tokens", sa.Integer, nullable=False),
    sa.Column("max_output_tokens", sa.Integer, nullable=False),
    sa.Column("supports_tools", sa.Boolean, server_default=sa.text("false"), nullable=False),
    sa.Column(
        "supports_structured_output", sa.Boolean, server_default=sa.text("false"), nullable=False
    ),
    sa.Column("supports_streaming", sa.Boolean, server_default=sa.text("true"), nullable=False),
    sa.Column("supports_thinking", sa.Boolean, server_default=sa.text("false"), nullable=False),
    sa.Column("geographic_regions", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
    sa.Column(
        "requires_disclosure_approval", sa.Boolean, server_default=sa.text("true"), nullable=False
    ),
    sa.Column("default_price_schedule_version", sa.String(32), nullable=False),
    created(),
    pk("provider", "model_id", "revision"),
)

model_reservations = sa.Table(
    "model_reservations",
    md,
    orgid(),
    col("reservation_id", "uuid"),
    col("repository_id", "uuid"),
    col("principal_id", "uuid"),
    col("investigation_id", "uuid"),
    col("logical_call_id", "uuid"),
    sa.Column("attempt_id", sa.Integer, nullable=False),
    sa.Column("provider", sa.String(64), nullable=False),
    sa.Column("model_id", sa.String(128), nullable=False),
    sa.Column("charge_category", sa.String(32), server_default="generation", nullable=False),
    sa.Column("estimated_input_tokens", sa.Integer, nullable=False),
    sa.Column("estimated_output_tokens", sa.Integer, nullable=False),
    sa.Column("reserved_micros", sa.BigInteger, nullable=False),
    sa.Column("settled_micros", sa.BigInteger, nullable=True),
    sa.Column("state", sa.String(32), nullable=False),
    sa.Column("idempotency_key", sa.String(128), nullable=False),
    sa.Column("provider_request_id", sa.String(128), nullable=True),
    sa.Column("failure_reason", sa.Text, nullable=True),
    created(),
    sa.Column(
        "updated_at",
        sa.TIMESTAMP(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    pk("org_id", "reservation_id"),
    uq("uq_model_reservation_idempotency", "org_id", "idempotency_key"),
    ck(
        "ck_model_reservation_state",
        "state IN ('reserved', 'dispatch_intent_committed', 'dispatched', 'settled', "
        "'rejected_before_dispatch', 'uncertain_hold', 'conservatively_reconciled')",
    ),
    ck("ck_model_reservation_micros_pos", "reserved_micros >= 0"),
    fk(
        "fk_model_reservation_member",
        ["org_id", "principal_id"],
        "memberships",
        ["org_id", "principal_id"],
    ),
    fk(
        "fk_model_reservation_repo",
        ["org_id", "repository_id"],
        "repositories",
        ["org_id", "id"],
    ),
)

model_spend_ledger = sa.Table(
    "model_spend_ledger",
    md,
    orgid(),
    col("id", "uuid"),
    col("repository_id", "uuid"),
    col("reservation_id", "uuid"),
    col("investigation_id", "uuid"),
    sa.Column("attempt_id", sa.Integer, nullable=False),
    sa.Column("provider", sa.String(64), nullable=False),
    sa.Column("model_id", sa.String(128), nullable=False),
    sa.Column("charge_category", sa.String(32), nullable=False),
    sa.Column("input_tokens", sa.Integer, server_default="0", nullable=False),
    sa.Column("output_tokens", sa.Integer, server_default="0", nullable=False),
    sa.Column("thinking_tokens", sa.Integer, server_default="0", nullable=False),
    sa.Column("charged_micros", sa.BigInteger, nullable=False),
    sa.Column("is_uncertain_hold", sa.Boolean, server_default=sa.text("false"), nullable=False),
    created(),
    pk("org_id", "id"),
    fk(
        "fk_spend_ledger_repo",
        ["org_id", "repository_id"],
        "repositories",
        ["org_id", "id"],
    ),
)
