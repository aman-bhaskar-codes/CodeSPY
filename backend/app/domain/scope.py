from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class OrgRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class TenantContext(Contract):
    org_id: UUID
    principal_id: UUID
    role: OrgRole


class RunRef(Contract):
    org_id: UUID
    snapshot_id: UUID
    run_id: UUID
