import os

import pytest
import pytest_asyncio
from app.core.database_config import DatabaseSettings
from app.db.guard import validate_target
from app.db.session import build_engine, build_session_factory
from sqlalchemy.engine import URL


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "schema_revision(name): qualify an owned historical migration boundary"
    )


@pytest.fixture
def db_values():
    keys = [
        "HOST",
        "PORT",
        "DATABASE",
        "MARKER",
        "PROJECT",
        "APP_PASSWORD",
        "MIGRATION_PASSWORD",
        "ADMIN_PASSWORD",
    ]
    values = {f"CODESPY_TEST_{k}": os.environ.get(f"CODESPY_TEST_{k}", "") for k in keys}
    validate_target(
        host=values["CODESPY_TEST_HOST"],
        port=int(values["CODESPY_TEST_PORT"] or "0"),
        database=values["CODESPY_TEST_DATABASE"],
        marker=values["CODESPY_TEST_MARKER"],
        project=values["CODESPY_TEST_PROJECT"],
    )
    return values


@pytest_asyncio.fixture
async def engine(db_values):
    url = URL.create(
        "postgresql+asyncpg",
        username="p1_app",
        password=db_values["CODESPY_TEST_APP_PASSWORD"],
        host=db_values["CODESPY_TEST_HOST"],
        port=int(db_values["CODESPY_TEST_PORT"]),
        database=db_values["CODESPY_TEST_DATABASE"],
    )
    engine = build_engine(DatabaseSettings(DATABASE_URL=url.render_as_string(hide_password=False)))
    yield engine
    await engine.dispose()


@pytest.fixture
def factory(engine):
    return build_session_factory(engine)
