import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from tests.phase2.integration.test_database_authority import active_admission


async def test_api_role_cannot_turn_worker_settings_into_publication_authority(factory, db_values):
    ctx, _snap, _, job, attempt = await active_admission(factory)
    url = factory.kw["bind"].url.set(
        username="p2_api", password=db_values["CODESPY_TEST_APP_PASSWORD"]
    )
    engine = create_async_engine(url)
    try:
        with pytest.raises(DBAPIError) as error:
            async with engine.begin() as connection:
                for name, value in [
                    ("org_id", ctx.org_id),
                    ("job_id", job),
                    ("attempt_id", attempt),
                    ("fence", 1),
                ]:
                    await connection.execute(
                        text("SELECT set_config(:key,:value,true)"),
                        {"key": "codespy." + name, "value": str(value)},
                    )
                await connection.execute(
                    text(
                        "UPDATE jobs SET state='succeeded',finished_at=clock_timestamp() "
                        "WHERE org_id=:org AND id=:job"
                    ),
                    {"org": ctx.org_id, "job": job},
                )
        assert getattr(error.value.orig, "sqlstate", None) == "42501"
    finally:
        await engine.dispose()
