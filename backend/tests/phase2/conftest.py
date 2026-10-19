"""Phase 2 fixtures accept only the separately owned disposable destination."""

import os

import pytest
import pytest_asyncio
from app.db.phase2_guard import validate_target
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
def db_values():
    names = [
        "HOST",
        "PORT",
        "DATABASE",
        "MARKER",
        "PROJECT",
        "APP_PASSWORD",
        "BROKER_PASSWORD",
        "MIGRATION_PASSWORD",
        "ADMIN_PASSWORD",
    ]
    values = {f"CODESPY_TEST_{n}": os.environ.get(f"CODESPY_TEST_{n}", "") for n in names}
    validate_target(
        host=values["CODESPY_TEST_HOST"],
        port=int(values["CODESPY_TEST_PORT"] or "0"),
        database=values["CODESPY_TEST_DATABASE"],
        marker=values["CODESPY_TEST_MARKER"],
        project=values["CODESPY_TEST_PROJECT"],
    )
    # Capacity and recovery are global database invariants. Give each live test
    # its own migrated, marker-owned database instead of sharing stale fixtures.
    from scripts import phase2_db as harness

    database = harness.create_database(values, "case")
    try:
        harness.migrate(
            values, database, os.environ.get("CODESPY_TEST_SCHEMA_TARGET", "0009_api_commands")
        )
        harness.grant_app(values, database)
        yield {**values, "CODESPY_TEST_DATABASE": database}
    finally:
        harness.drop_database(values, database)


@pytest_asyncio.fixture
async def engine(db_values):
    url = URL.create(
        "postgresql+asyncpg",
        username="p2_migration",
        password=db_values["CODESPY_TEST_MIGRATION_PASSWORD"],
        host=db_values["CODESPY_TEST_HOST"],
        port=int(db_values["CODESPY_TEST_PORT"]),
        database=db_values["CODESPY_TEST_DATABASE"],
    )
    engine = create_async_engine(
        url, connect_args={"command_timeout": 20}, pool_size=5, max_overflow=0
    )
    yield engine
    await engine.dispose()


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)
