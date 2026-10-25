"""Internal receipt contracts; evidence references contain no provider payload."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from app.contracts.model import TokenUsageV1
from app.domain.scope import Contract


class ReconcileModelUsageV1(Contract):
    reservation_id: UUID
    request_key: str = Field(min_length=1, max_length=128)
    provider_request_id: str = Field(min_length=1, max_length=128)
    evidence_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    usage: TokenUsageV1
    reason: Literal["provider_usage_receipt", "operator_usage_confirmation"]


class ModelReconciliationReceiptV1(Contract):
    audit_id: UUID
    reservation_id: UUID
    charged_micros: int = Field(ge=0)
    evidence_digest: str
