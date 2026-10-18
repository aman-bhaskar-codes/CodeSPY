"""Database-only configuration. No implicit dotenv reads or connections."""

from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)
    DATABASE_URL: SecretStr | None = Field(default=None, repr=False)
    DATABASE_MIGRATION_URL: SecretStr | None = Field(default=None, repr=False)
    POSTGRES_HOST: str | None = None
    POSTGRES_PORT: int | None = Field(default=None, ge=1, le=65535)
    POSTGRES_DB: str | None = None
    POSTGRES_USER: str | None = None
    POSTGRES_PASSWORD: SecretStr | None = Field(default=None, repr=False)
    DB_POOL_SIZE: int = Field(default=5, ge=1, le=20)
    DB_MAX_OVERFLOW: int = Field(default=0, ge=0, le=10)
    DB_POOL_TIMEOUT_SECONDS: int = Field(default=5, gt=0)
    DB_CONNECT_TIMEOUT_SECONDS: int = Field(default=5, gt=0)
    DB_STATEMENT_TIMEOUT_MS: int = Field(default=10000, gt=0)
    DB_LOCK_TIMEOUT_MS: int = Field(default=2000, gt=0)
    DB_IDLE_TRANSACTION_TIMEOUT_MS: int = Field(default=15000, gt=0)

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        if (
            not self.DB_LOCK_TIMEOUT_MS
            < self.DB_STATEMENT_TIMEOUT_MS
            < self.DB_IDLE_TRANSACTION_TIMEOUT_MS
        ):
            raise ValueError("require lock < statement < idle transaction timeout")
        try:
            url = self.async_url
            if url.drivername != "postgresql+asyncpg" or not url.database or not url.host:
                raise ValueError("invalid database destination")
        except Exception:
            raise ValueError("complete PostgreSQL connection settings required") from None
        return self

    @property
    def async_url(self) -> URL:
        if self.DATABASE_URL and self.DATABASE_URL.get_secret_value().strip():
            return make_url(self.DATABASE_URL.get_secret_value()).set(
                drivername="postgresql+asyncpg"
            )
        if not all(
            (
                self.POSTGRES_HOST,
                self.POSTGRES_PORT,
                self.POSTGRES_DB,
                self.POSTGRES_USER,
                self.POSTGRES_PASSWORD,
            )
        ):
            raise ValueError("complete local connection fields required")
        return URL.create(
            "postgresql+asyncpg",
            username=self.POSTGRES_USER,
            password=self.POSTGRES_PASSWORD.get_secret_value() if self.POSTGRES_PASSWORD else None,
            host=self.POSTGRES_HOST,
            port=self.POSTGRES_PORT,
            database=self.POSTGRES_DB,
        )

    @property
    def migration_url(self) -> URL:
        if self.DATABASE_MIGRATION_URL and self.DATABASE_MIGRATION_URL.get_secret_value().strip():
            return make_url(self.DATABASE_MIGRATION_URL.get_secret_value()).set(
                drivername="postgresql+psycopg"
            )
        return self.async_url.set(drivername="postgresql+psycopg")
