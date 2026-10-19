"""Public inventory contracts contain no storage keys, leases or provider tokens."""

from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.domain.scope import Contract


class SessionV1(Contract):
    schema_version: Literal[1] = 1
    principal_id: UUID
    csrf_token: str
    auth_backend: Literal["disabled", "local_fixture", "github_app"]


class ErrorV1(Contract):
    schema_version: Literal[1] = 1
    code: str = Field(max_length=128)
    message: str = Field(max_length=256)
    request_id: UUID
    retryable: bool


class RepositoryListItemV1(Contract):
    id: UUID
    org_id: UUID
    display_name: str
    source_kind: Literal["local_fixture", "github", "archive"]


class RepositoryPageV1(Contract):
    items: list[RepositoryListItemV1]
    next_cursor: str | None = None


class InventoryEntryV1(Contract):
    id: UUID
    path: str
    sha256: str
    size_bytes: int
    language: str | None
    eligible: bool
    exclusion_reason: str | None


class InventoryPageV1(Contract):
    items: list[InventoryEntryV1]
    next_cursor: str | None = None
    data_version: Literal[1] = 1
    snapshot_id: UUID


class JobEventV2(Contract):
    schema_version: Literal[2] = 2
    job_id: UUID
    sequence: int = Field(gt=0)
    event_type: Literal[
        "submitted",
        "upload_verified",
        "queued",
        "commit_pinned",
        "attempt_started",
        "progress",
        "retry_scheduled",
        "cancel_requested",
        "cancelled",
        "admitted",
        "inventory_completed",
        "inventory_partial",
        "failed",
    ]
    public_payload: dict[str, Any]
    created_at: AwareDatetime

    @model_validator(mode="after")
    def safe_payload(self) -> "JobEventV2":
        allowed = {
            "file_count",
            "eligible_count",
            "excluded_count",
            "error_code",
            "stage",
            "commit_sha",
        }
        if set(self.public_payload) - allowed:
            raise ValueError("unexpected public event field")
        for key, value in self.public_payload.items():
            if key.endswith("_count") and (type(value) is not int or value < 0):
                raise ValueError("invalid event count")
            if key in {"error_code", "stage"} and (
                not isinstance(value, str)
                or not value.isascii()
                or not value.replace("_", "").isalnum()
                or len(value) > 128
            ):
                raise ValueError("invalid event code")
            if key == "commit_sha" and (
                not isinstance(value, str)
                or len(value) != 40
                or any(c not in "0123456789abcdef" for c in value)
            ):
                raise ValueError("invalid commit identity")
        return self


class EventPageV2(Contract):
    items: list[JobEventV2]
    next_cursor: str | None = None
    data_version: Literal[2] = 2


class GitHubAcquisitionV1(Contract):
    requested_ref: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_./-]+$")


class GitHubBindingV1(Contract):
    github_repository_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)
