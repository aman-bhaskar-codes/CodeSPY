"""Immutable receipts for bounded post-publication memory derivation."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from app.models.phase2_tables import col, created, fk, md, orgid, pk, uq

report_memory_receipts = sa.Table(
    "report_memory_receipts",
    md,
    orgid(),
    col("intent_id", "uuid"),
    col("report_id", "uuid", True),
    col("report_digest", "text", True),
    sa.Column("derivation_revision", sa.Integer, nullable=False),
    sa.Column("outcome", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=True),
    sa.Column("candidate_ids", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
    sa.Column("omitted_claims", sa.Integer, nullable=False, server_default="0"),
    created(),
    pk("org_id", "intent_id"),
    uq("uq_report_memory_origin", "org_id", "report_id", "derivation_revision"),
    fk("fk_report_memory_intent", ["org_id", "intent_id"], "outbox", ["org_id", "id"]),
    fk(
        "fk_report_memory_report",
        ["org_id", "report_id"],
        "investigation_reports",
        ["org_id", "id"],
    ),
)
