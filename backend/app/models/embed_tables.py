"""Independent projection of the shared jobs table for the dense embed operation."""

import sqlalchemy as sa

from app.models.index_tables import index_jobs
from app.models.phase2_tables import col

embed_jobs = sa.Table(
    "jobs",
    sa.MetaData(),
    *[
        sa.Column(
            c.name,
            c.type,
            nullable=c.nullable,
            primary_key=c.primary_key,
            server_default=c.server_default,
        )
        for c in index_jobs.columns
        if c.name != "index_spec_id"
    ],
    col("embed_spec_id", "uuid", True),
)
