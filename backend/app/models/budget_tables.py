"""SQLAlchemy tables for embedding provider budgets, reservations, and price schedules."""

import sqlalchemy as sa

from app.models.phase2_tables import ck, col, created, fk, md, orgid, pk, uq

embedding_budgets = sa.Table(
    "embedding_budgets",
    md,
    orgid(),
    col("repository_id", "uuid"),
    sa.Column("currency", sa.String(3), server_default="USD", nullable=False),
    sa.Column("balance_micros", sa.BigInteger, nullable=False),
    sa.Column("reserved_micros", sa.BigInteger, server_default="0", nullable=False),
    sa.Column("write_version", sa.BigInteger, server_default="1", nullable=False),
    created(),
    sa.Column(
        "updated_at",
        sa.TIMESTAMP(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    pk("org_id", "repository_id"),
    ck("ck_budget_balance_nonneg", "balance_micros >= 0"),
    ck("ck_budget_reserved_nonneg", "reserved_micros >= 0"),
    ck("ck_budget_reserved_le_balance", "reserved_micros <= balance_micros"),
    fk("fk_budget_repo", ["org_id", "repository_id"], "repositories", ["org_id", "id"]),
)

embedding_reservations = sa.Table(
    "embedding_reservations",
    md,
    orgid(),
    col("reservation_id", "uuid"),
    col("repository_id", "uuid"),
    col("principal_id", "uuid"),
    sa.Column("provider", sa.String(64), nullable=False),
    sa.Column("model_id", sa.String(128), nullable=False),
    sa.Column("estimated_tokens", sa.Integer, nullable=False),
    sa.Column("estimated_micros", sa.BigInteger, nullable=False),
    sa.Column("settled_micros", sa.BigInteger, nullable=True),
    sa.Column("state", sa.String(32), nullable=False),
    sa.Column("idempotency_key", sa.String(128), nullable=False),
    sa.Column("failure_reason", sa.Text, nullable=True),
    created(),
    sa.Column(
        "updated_at",
        sa.TIMESTAMP(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    pk("org_id", "reservation_id"),
    uq("uq_reservation_idempotency", "org_id", "idempotency_key"),
    ck(
        "ck_reservation_state",
        "state IN ('reserved', 'committed', 'released', 'uncertain_hold')",
    ),
    ck("ck_reservation_tokens_pos", "estimated_tokens > 0"),
    ck("ck_reservation_micros_pos", "estimated_micros > 0"),
    fk(
        "fk_reservation_member",
        ["org_id", "principal_id"],
        "memberships",
        ["org_id", "principal_id"],
    ),
    fk(
        "fk_reservation_repo",
        ["org_id", "repository_id"],
        "repositories",
        ["org_id", "id"],
    ),
)

price_schedules = sa.Table(
    "price_schedules",
    md,
    sa.Column("provider", sa.String(64), nullable=False),
    sa.Column("model_id", sa.String(128), nullable=False),
    sa.Column("version", sa.String(32), nullable=False),
    sa.Column("input_micros_per_1k", sa.BigInteger, nullable=False),
    sa.Column("output_micros_per_1k", sa.BigInteger, server_default="0", nullable=False),
    sa.Column("thinking_micros_per_1k", sa.BigInteger, server_default="0", nullable=False),
    sa.Column("cache_read_micros_per_1k", sa.BigInteger, server_default="0", nullable=False),
    sa.Column("cache_write_micros_per_1k", sa.BigInteger, server_default="0", nullable=False),
    sa.Column("effective_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
    pk("provider", "model_id", "version"),
    ck("ck_price_rate_pos", "input_micros_per_1k > 0"),
    ck("ck_price_effective_before_expires", "effective_at < expires_at"),
)
