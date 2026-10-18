from datetime import datetime

import pytest
from app.contracts.persistence import CoverageV1
from app.core.database_config import DatabaseSettings
from app.domain.canonical_json import canonical_bytes, load_json, semantic_digest
from app.domain.source_ranges import SourceRangeV1
from pydantic import ValidationError


def test_settings_special_password():
    s = DatabaseSettings(
        POSTGRES_HOST="127.0.0.1",
        POSTGRES_PORT=5432,
        POSTGRES_DB="codespy",
        POSTGRES_USER="local",
        POSTGRES_PASSWORD="p%@:é",
        DATABASE_URL="",
    )
    assert s.async_url.password == "p%@:é"
    assert s.migration_url.drivername == "postgresql+psycopg"
    assert "p%@:é" not in repr(s)


def test_settings_require_complete_connection():
    with pytest.raises(ValidationError):
        DatabaseSettings(_env_file=None)


@pytest.mark.parametrize(
    "values", [{"DB_POOL_SIZE": 0}, {"DB_LOCK_TIMEOUT_MS": 10000}, {"DB_MAX_OVERFLOW": 11}]
)
def test_settings_bounds(values):
    with pytest.raises(ValidationError):
        DatabaseSettings(DATABASE_URL="postgresql://a:b@localhost/local", **values)


def test_canonical_json():
    assert canonical_bytes({"b": 1, "a": "é"}) == b'{"a":"\xc3\xa9","b":1}'
    assert canonical_bytes(1.25) == b"1.25"
    assert semantic_digest({"a": 1}) != semantic_digest({"a": 2})
    with pytest.raises(ValueError):
        canonical_bytes(float("nan"))
    with pytest.raises(ValueError):
        load_json('{"a":1,"a":2}')
    with pytest.raises(ValueError):
        canonical_bytes(datetime.now())


def test_ranges_and_coverage():
    assert SourceRangeV1(start_byte=0, end_byte=0, start_line=1, end_line=1, start_col=0, end_col=0)
    with pytest.raises(ValidationError):
        SourceRangeV1(start_byte=1, end_byte=0, start_line=1, end_line=1, start_col=0, end_col=0)
    with pytest.raises(ValidationError):
        CoverageV1(
            status="complete",
            scope_description="fixture",
            admitted_count=2,
            processed_count=1,
            omitted_count=1,
            truncated=False,
        )
    with pytest.raises(ValidationError):
        SourceRangeV1(start_byte=0)


def test_unrelated_dotenv_and_secret_redaction(tmp_path, monkeypatch):
    path = tmp_path / "database.env"
    path.write_text(
        "DATABASE_URL=postgresql://fixture:p%25%40%3A%C3%A9@localhost/local\nUNRELATED_SETTING=ignored\n"
    )
    s = DatabaseSettings(_env_file=path)
    assert s.async_url.password == "p%@:é"
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture:override@localhost/local")
    assert DatabaseSettings(_env_file=path).async_url.password == "override"
