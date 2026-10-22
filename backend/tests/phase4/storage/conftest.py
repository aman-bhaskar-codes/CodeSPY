"""Owned per-case scan databases with separately checked API and broker roles."""

import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
def db_values():
    from scripts import phase2_db as harness

    values = {key: value for key, value in os.environ.items() if key.startswith("CODESPY_TEST_")}
    if not values.get("CODESPY_TEST_MARKER"):
        pytest.skip("owned scan database environment required")
    name = harness.create_database(values, "scancase")
    try:
        harness.migrate(
            values, name, values.get("CODESPY_TEST_SCHEMA_TARGET", "0014_finding_triage")
        )
        harness.grant_app(values, name)
        yield {**values, "CODESPY_TEST_DATABASE": name}
    finally:
        harness.drop_database(values, name)


async def role_factory(values, role, password_key):
    url = URL.create(
        "postgresql+asyncpg",
        username=role,
        password=values[password_key],
        host=values["CODESPY_TEST_HOST"],
        port=int(values["CODESPY_TEST_PORT"]),
        database=values["CODESPY_TEST_DATABASE"],
    )
    engine = create_async_engine(
        url, connect_args={"command_timeout": 20}, pool_size=5, max_overflow=0
    )
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT current_user")) == role
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def api_factory(db_values):
    async for factory in role_factory(db_values, "p2_api", "CODESPY_TEST_APP_PASSWORD"):
        yield factory


@pytest_asyncio.fixture
async def broker_factory(db_values):
    async for factory in role_factory(db_values, "p2_broker", "CODESPY_TEST_BROKER_PASSWORD"):
        yield factory


@pytest_asyncio.fixture
async def migration_factory(db_values):
    async for factory in role_factory(db_values, "p2_migration", "CODESPY_TEST_MIGRATION_PASSWORD"):
        yield factory
