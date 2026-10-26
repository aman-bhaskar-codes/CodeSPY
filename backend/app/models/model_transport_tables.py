"""Immutable numeric inference receipts and encrypted protocol continuation state."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from app.models.phase2_tables import col, created, fk, md, orgid, pk, uq

provider_usage_receipts = sa.Table(
    "provider_usage_receipts",
    md,
    orgid(),
    col("id", "uuid"),
    col("repository_id", "uuid"),
    col("reservation_id", "uuid"),
    sa.Column("profile_digest", sa.String(64), nullable=False),
    sa.Column("payload_digest", sa.String(64), nullable=False),
    sa.Column("quality", sa.String(32), nullable=False),
    sa.Column("evidence_json", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
    created(),
    pk("org_id", "id"),
    uq("uq_provider_usage_evidence", "org_id", "reservation_id", "payload_digest"),
    fk(
        "fk_provider_usage_reservation",
        ["org_id", "reservation_id"],
        "model_reservations",
        ["org_id", "reservation_id"],
    ),
)
sa.Index(
    "uq_provider_final_usage",
    provider_usage_receipts.c.org_id,
    provider_usage_receipts.c.reservation_id,
    unique=True,
    postgresql_where=provider_usage_receipts.c.quality != "unknown",
    sqlite_where=provider_usage_receipts.c.quality != "unknown",
)

model_continuations = sa.Table(
    "model_continuations",
    md,
    orgid(),
    col("id", "uuid"),
    col("repository_id", "uuid"),
    col("reservation_id", "uuid"),
    col("investigation_id", "uuid"),
    col("principal_id", "uuid"),
    sa.Column("profile_digest", sa.String(64), nullable=False),
    sa.Column("ciphertext", sa.LargeBinary, nullable=False),
    sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
    created(),
    pk("org_id", "id"),
    fk(
        "fk_model_continuation_reservation",
        ["org_id", "reservation_id"],
        "model_reservations",
        ["org_id", "reservation_id"],
    ),
)
