"""Keep the migration ledger compatible with existing descriptive revision IDs."""

from sqlalchemy import Column, Connection, MetaData, String, Table, text


def ensure_revision_capacity(connection: Connection) -> None:
    version = Table(
        "alembic_version",
        MetaData(),
        Column("version_num", String(64), nullable=False, primary_key=True),
        schema="public",
    )
    version.create(connection, checkfirst=True)
    capacity = connection.scalar(
        text(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='alembic_version' "
            "AND column_name='version_num'"
        )
    )
    if capacity is not None and capacity < 64:
        connection.execute(
            text("ALTER TABLE public.alembic_version ALTER COLUMN version_num TYPE varchar(64)")
        )
