from alembic import context
from app.core.database_config import DatabaseSettings
from app.db.base import Base
from app.models import phase2_tables, phase3_tables, tables  # noqa: F401
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

settings = DatabaseSettings()
url = context.config.attributes.get("connection_url") or settings.migration_url
if url.host not in {"127.0.0.1", "localhost", "::1"}:
    raise ValueError("Phase 1 migrations require a local PostgreSQL destination")
engine = create_engine(
    url,
    poolclass=NullPool,
    hide_parameters=True,
    connect_args={"options": "-c timezone=UTC -c statement_timeout=60000 -c lock_timeout=5000"},
)
with engine.connect() as connection:
    version = connection.scalar(text("SHOW server_version_num"))
    extensions = set(connection.scalars(text("SELECT extname FROM pg_extension")))
    connection.rollback()
    if (
        not 160000 <= int(version) < 170000
        or not {"vector", "pg_trgm", "citext", "uuid-ossp"} <= extensions
    ):
        raise RuntimeError("PostgreSQL 16 and provisioned extensions required")
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
