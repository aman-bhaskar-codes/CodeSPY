"""Session and command contracts for the agent API (AEP/1, section 5)."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from app.contracts.agent_events import ContextChip, Mode, SessionLimits, SessionStatus
from app.domain.scope import Contract

DEFAULT_MAX_STEPS = 60
DEFAULT_MAX_TOOL_CALLS = 150
DEFAULT_COST_MICROS_LIMIT = 5_000_000  # USD 5 per session, owner-tunable

ProfileName = Literal["agent", "fast", "deep"]
DEFAULT_MODEL_PROFILE: ProfileName = "agent"


class AgentSessionCreateRequestV1(Contract):
    repository_id: UUID
    snapshot_id: UUID
    mode: Mode = "plan"
    model_profile: ProfileName = DEFAULT_MODEL_PROFILE


class AgentSessionV1(Contract):
    id: UUID
    repository_id: UUID
    snapshot_id: UUID
    mode: Mode
    status: SessionStatus
    model_profile: ProfileName
    last_seq: int = Field(ge=0)
    created_at: str


class AgentMessageRequestV1(Contract):
    text: Annotated[str, Field(min_length=1, max_length=32768)]
    context: tuple[ContextChip, ...] = Field(default=(), max_length=50)
    queue: bool = False


class AgentApprovalDecisionV1(Contract):
    decision: Literal["approved", "rejected"]
    scope: Literal["once", "session_rule"] = "once"
    rule: Annotated[str, Field(max_length=256)] | None = None


class AgentPatchDecisionV1(Contract):
    decision: Literal["apply", "reject"]
    hunks: tuple[Annotated[str, Field(max_length=128)], ...] | None = Field(
        default=None, max_length=1000
    )


class AgentModeRequestV1(Contract):
    mode: Mode


class AgentRestoreRequestV1(Contract):
    restore: Literal["files", "conversation", "both"] = "both"


class AgentFilePutV1(Contract):
    path: Annotated[str, Field(min_length=1, max_length=1024)]
    base_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    content: Annotated[str, Field(max_length=1_048_576)]


class AgentToolSummaryV1(Contract):
    tool_call_id: str
    tool: str
    status: Literal[
        "proposed",
        "running",
        "ok",
        "partial",
        "unavailable",
        "denied",
        "invalid",
        "timed_out",
        "outcome_unknown",
    ]
    summary: str = ""


class AgentMessageViewV1(Contract):
    message_id: str
    role: Literal["user", "assistant"]
    text: str
    completed: bool


class AgentApprovalViewV1(Contract):
    approval_id: str
    subject_kind: str
    subject_ref: str
    status: Literal["pending", "approved", "rejected", "expired"]


class AgentStateV1(Contract):
    """A compacted fold of the event log. ``as_of_seq`` is the last event the snapshot includes."""

    session_id: UUID
    as_of_seq: int = Field(ge=0)
    status: SessionStatus = "idle"
    mode: Mode = "plan"
    turn: int = Field(default=0, ge=0)
    limits: SessionLimits | None = None
    messages: tuple[AgentMessageViewV1, ...] = ()
    tools: tuple[AgentToolSummaryV1, ...] = ()
    approvals: tuple[AgentApprovalViewV1, ...] = ()
    checkpoints: tuple[str, ...] = ()
    last_error_code: str | None = None
