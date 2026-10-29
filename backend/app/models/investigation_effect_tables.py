"""Encrypted execution-effect metadata; reads and writes use scoped routines."""

import sqlalchemy as sa

from app.models.phase2_tables import ck, col, created, fk, md, orgid, pk

investigation_effects = sa.Table(
    "investigation_effects",
    md,
    orgid(),
    col("investigation_id", "uuid"),
    col("id", "uuid"),
    col("binding_digest"),
    col("kind"),
    col("input_digest"),
    col("payload_digest"),
    sa.Column("payload_bytes", sa.Integer, nullable=False),
    sa.Column("ciphertext", sa.LargeBinary, nullable=True),
    created(),
    col("expires_at", "time"),
    pk("org_id", "investigation_id", "id"),
    fk(
        "fk_effect_investigation",
        ["org_id", "investigation_id"],
        "investigations",
        ["org_id", "id"],
    ),
    ck("ck_effect_kind", "kind IN ('model','tool','draft')"),
    ck("ck_effect_payload_bound", "payload_bytes BETWEEN 1 AND 524288"),
)
