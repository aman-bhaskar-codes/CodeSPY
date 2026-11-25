"""AEP/1: the event contract between the agent runtime and the browser.

Every event, stored and streamed, uses one envelope. The payload is validated against the model
registered for its ``type``. Payloads are at most 16 KiB; larger content is referenced,
never inlined.
Additive changes (new types, new optional fields) stay in version 1; clients ignore unknown types.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from app.domain.scope import Contract

PROTOCOL_VERSION = 1
MAX_PAYLOAD_BYTES = 16 * 1024
REPLAY_LIMIT = 2000
HEARTBEAT_SECONDS = 15.0

Mode = Literal["plan", "act"]
SessionStatus = Literal[
    "idle", "running", "waiting_approval", "interrupted", "completed", "failed", "cancelled"
]
ToolClass = Literal["read", "edit", "exec", "git", "network", "mcp", "re"]
Risk = Literal["low", "medium", "high"]
ToolStatus = Literal[
    "ok", "partial", "unavailable", "denied", "invalid", "timed_out", "outcome_unknown"
]
ClaimKind = Literal["observation", "derivation", "inference", "unknown"]
VerifierState = Literal["verified", "unverified", "contradicted"]

ID = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
Path = Annotated[str, Field(min_length=1, max_length=1024)]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
HexOffset = Annotated[str, Field(pattern=r"^[0-9a-f]{1,16}$")]
Text = Annotated[str, Field(max_length=8192)]


class _Payload(Contract):
    """Base of every payload model."""


# --- shared parts -----------------------------------------------------------------------------


class ContextChip(_Payload):
    kind: Literal["file", "symbol", "evidence", "terminal", "selection"]
    ref: Annotated[str, Field(min_length=1, max_length=512)]
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class SourceRangeProvenance(_Payload):
    kind: Literal["source_range"] = "source_range"
    path: Path
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class ByteRangeProvenance(_Payload):
    kind: Literal["byte_range"] = "byte_range"
    artifact_id: ID
    start_offset: HexOffset
    end_offset: HexOffset


class ReportFactProvenance(_Payload):
    kind: Literal["report_fact"] = "report_fact"
    report_id: ID
    statement_id: Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]


class ToolResultProvenance(_Payload):
    kind: Literal["tool_result"] = "tool_result"
    tool_call_id: ID


Provenance = Annotated[
    SourceRangeProvenance | ByteRangeProvenance | ReportFactProvenance | ToolResultProvenance,
    Field(discriminator="kind"),
]


class EvidenceRef(_Payload):
    evidence_id: ID
    provenance: Provenance | None = None


class ClaimRef(_Payload):
    claim_id: ID


class Usage(_Payload):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)


class Hunk(_Payload):
    hunk_id: ID
    old_start: int = Field(ge=0)
    old_lines: int = Field(ge=0)
    new_start: int = Field(ge=0)
    new_lines: int = Field(ge=0)
    content_ref: ID | None = None
    preview: Annotated[str, Field(max_length=2048)] = ""


class DiffFile(_Payload):
    path: Path
    change: Literal["modify", "create", "delete", "rename"]
    before_hash: Hash | None = None
    after_hash: Hash | None = None
    renamed_from: Path | None = None
    hunks: tuple[Hunk, ...] = Field(default=(), max_length=200)


class PlanStep(_Payload):
    id: ID
    title: Annotated[str, Field(min_length=1, max_length=500)]
    status: Literal["pending", "in_progress", "completed", "skipped"] = "pending"


class TestFailure(_Payload):
    __test__ = False
    name: Annotated[str, Field(max_length=512)]
    file: Path | None = None
    line: int | None = Field(default=None, ge=1)
    message: Annotated[str, Field(max_length=2048)] = ""


class Limits(_Payload):
    max_steps: int = Field(ge=1)
    max_tool_calls: int = Field(ge=1)
    cost_micros_limit: int = Field(ge=0)


# --- payloads, one per event type -------------------------------------------------------------


class SessionCreated(_Payload):
    mode: Mode
    workspace_id: ID | None = None
    base_snapshot_id: ID
    model_profile: ID
    limits: Limits


class SessionStatusChanged(_Payload):
    status: SessionStatus
    reason: Annotated[str, Field(max_length=256)] | None = None


class SessionModeChanged(_Payload):
    mode: Mode
    by: Literal["user"] = "user"


class SessionLimits(_Payload):
    steps_used: int = Field(ge=0)
    steps_max: int = Field(ge=1)
    tool_calls_used: int = Field(ge=0)
    tool_calls_max: int = Field(ge=1)
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    cost_micros_reserved: int = Field(ge=0)
    cost_micros_settled: int = Field(ge=0)
    cost_micros_limit: int = Field(ge=0)


class StreamReset(_Payload):
    reason: Literal["replay_limit", "backpressure", "schema_upgrade"]


class MessageUser(_Payload):
    message_id: ID
    text: Annotated[str, Field(max_length=32768)]
    context: tuple[ContextChip, ...] = Field(default=(), max_length=50)


class MessageStarted(_Payload):
    message_id: ID
    role: Literal["assistant"] = "assistant"
    model_id: ID


class MessageDelta(_Payload):
    message_id: ID
    offset: int = Field(ge=0)
    text: Annotated[str, Field(max_length=4096)]


class MessageCompleted(_Payload):
    message_id: ID
    text: Annotated[str, Field(max_length=65536)]
    stop_reason: ID
    usage: Usage | None = None
    claims: tuple[ClaimRef, ...] = Field(default=(), max_length=50)


class ThinkingSummary(_Payload):
    message_id: ID
    text: Annotated[str, Field(max_length=2048)]


class PlanProposed(_Payload):
    plan_id: ID
    steps: tuple[PlanStep, ...] = Field(max_length=50)
    requires_approval: bool = False


class PlanUpdated(_Payload):
    plan_id: ID
    steps: tuple[PlanStep, ...] = Field(max_length=50)


class PlanDecided(_Payload):
    plan_id: ID
    decision: Literal["approved", "rejected", "edited"]
    by: ID


class ToolCallProposed(_Payload):
    tool_call_id: ID
    tool: ID
    tool_class: ToolClass = Field(alias="class")
    args_preview: Annotated[str, Field(max_length=2048)] = ""
    risk: Risk = "low"


class ToolCallStarted(_Payload):
    tool_call_id: ID


class ToolCallOutput(_Payload):
    tool_call_id: ID
    stream: Literal["stdout", "stderr"]
    offset: int = Field(ge=0)
    text: Annotated[str, Field(max_length=4096)]


class ToolCallCompleted(_Payload):
    tool_call_id: ID
    status: ToolStatus
    summary: Annotated[str, Field(max_length=1024)] = ""
    truncated: bool = False
    bytes: int = Field(default=0, ge=0)
    redactions: int = Field(default=0, ge=0)
    content_ref: ID | None = None
    evidence: tuple[EvidenceRef, ...] = Field(default=(), max_length=50)


class ApprovalSubject(_Payload):
    kind: Literal["tool_call", "patch", "plan", "publish"]
    ref: ID


class ApprovalRequested(_Payload):
    approval_id: ID
    subject: ApprovalSubject
    reason: Annotated[str, Field(max_length=512)] = ""
    rule_matched: Annotated[str, Field(max_length=256)] | None = None
    expires_at: Annotated[str, Field(max_length=40)]


class ApprovalResolved(_Payload):
    approval_id: ID
    decision: Literal["approved", "rejected", "expired"]
    scope: Literal["once", "session_rule"] = "once"
    rule: Annotated[str, Field(max_length=256)] | None = None
    by: ID


class DiffProposed(_Payload):
    patch_id: ID
    files: tuple[DiffFile, ...] = Field(max_length=50)
    base_revision: int = Field(ge=0)


class DiffDecided(_Payload):
    patch_id: ID
    decision: Literal["applied", "rejected", "partially_applied"]
    hunks_applied: tuple[ID, ...] = Field(default=(), max_length=1000)
    by: ID


class WorkspaceRevision(_Payload):
    revision: int = Field(ge=0)
    changed_paths: tuple[Path, ...] = Field(default=(), max_length=200)
    author: Literal["agent", "user"]
    checkpoint_id: ID | None = None


class WorkspaceConflict(_Payload):
    path: Path
    expected_hash: Hash
    actual_hash: Hash
    author_of_actual: Literal["agent", "user"]


class CheckpointCreated(_Payload):
    checkpoint_id: ID
    revision: int = Field(ge=0)
    label: Annotated[str, Field(max_length=256)] = ""
    turn: int = Field(ge=0)


class CheckpointRestored(_Payload):
    checkpoint_id: ID
    restore: Literal["files", "conversation", "both"]


class TerminalOpened(_Payload):
    terminal_id: ID
    owner: Literal["agent", "user"]


class TerminalClosed(_Payload):
    terminal_id: ID
    owner: Literal["agent", "user"]
    exit_code: int | None = None


class TestResult(_Payload):
    __test__ = False
    tool_call_id: ID
    runner: ID
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    failures: tuple[TestFailure, ...] = Field(default=(), max_length=50)


class EvidenceClaim(_Payload):
    claim_id: ID
    kind: ClaimKind
    text: Annotated[str, Field(max_length=2048)]
    provenance: tuple[Provenance, ...] = Field(default=(), max_length=20)
    verifier: VerifierState = "unverified"


class ErrorPayload(_Payload):
    code: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")]
    message: Annotated[str, Field(max_length=512)]
    retryable: bool = False
    scope: Literal["turn", "session"] = "turn"


PAYLOADS: dict[str, type[_Payload]] = {
    "session.created": SessionCreated,
    "session.status": SessionStatusChanged,
    "session.mode.changed": SessionModeChanged,
    "session.limits": SessionLimits,
    "stream.reset": StreamReset,
    "message.user": MessageUser,
    "message.started": MessageStarted,
    "message.delta": MessageDelta,
    "message.completed": MessageCompleted,
    "thinking.summary": ThinkingSummary,
    "plan.proposed": PlanProposed,
    "plan.updated": PlanUpdated,
    "plan.decided": PlanDecided,
    "tool.call.proposed": ToolCallProposed,
    "tool.call.started": ToolCallStarted,
    "tool.call.output": ToolCallOutput,
    "tool.call.completed": ToolCallCompleted,
    "approval.requested": ApprovalRequested,
    "approval.resolved": ApprovalResolved,
    "diff.proposed": DiffProposed,
    "diff.decided": DiffDecided,
    "workspace.revision": WorkspaceRevision,
    "workspace.conflict": WorkspaceConflict,
    "checkpoint.created": CheckpointCreated,
    "checkpoint.restored": CheckpointRestored,
    "terminal.opened": TerminalOpened,
    "terminal.closed": TerminalClosed,
    "test.result": TestResult,
    "evidence.claim": EvidenceClaim,
    "error": ErrorPayload,
}
EVENT_TYPES = tuple(PAYLOADS)
TRANSIENT_TYPES = frozenset({"stream.reset"})  # delivered to one connection, never stored
_TYPE_PATTERN = re.compile(r"^[a-z]+(\.[a-z]+)*$")


def encode_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()


def validate_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate against the model registered for the type and return the canonical dict."""
    model = PAYLOADS.get(event_type)
    if model is None:
        raise ValueError(f"unknown event type {event_type!r}")
    parsed = model.model_validate_json(json.dumps(payload))
    canonical = parsed.model_dump(mode="json", by_alias=True, exclude_none=True)
    if len(encode_payload(canonical)) > MAX_PAYLOAD_BYTES:
        raise ValueError("event payload exceeds 16 KiB; store a blob and send a content_ref")
    return canonical


class AgentEventV1(Contract):
    """The envelope of every stored and streamed event."""

    v: Literal[1] = 1
    session_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    seq: int = Field(ge=0)
    type: str = Field(min_length=1, max_length=64)
    ts: str = Field(min_length=20, max_length=40)
    turn: int = Field(ge=0)
    parent: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    payload: dict[str, Any]

    @model_validator(mode="after")
    def _typed(self) -> AgentEventV1:
        if not _TYPE_PATTERN.fullmatch(self.type):
            raise ValueError("event type is a dotted lowercase name")
        if (self.seq == 0) != (self.type in TRANSIENT_TYPES):
            raise ValueError("only transient events carry seq 0; stored events start at 1")
        if self.type in PAYLOADS:
            validate_payload(self.type, self.payload)
        return self


def sse_frame(event: AgentEventV1) -> str:
    """One SSE frame. Transient events (seq 0) carry no id, so Last-Event-ID never regresses."""
    data = event.model_dump_json(exclude_none=True)
    head = f"id: {event.seq}\n" if event.seq else ""
    return f"{head}event: {event.type}\ndata: {data}\n\n"


HEARTBEAT_FRAME = ": hb\n\n"


# --- schema export ----------------------------------------------------------------------------


def export_schema() -> dict[str, Any]:
    """The JSON Schema file consumed by the frontend contract test and type generation."""
    defs: dict[str, Any] = {}
    payloads: dict[str, Any] = {}
    for name, model in PAYLOADS.items():
        schema = model.model_json_schema(by_alias=True, ref_template="#/$defs/{model}")
        defs.update(schema.pop("$defs", {}))
        payloads[name] = schema
    envelope = AgentEventV1.model_json_schema()
    defs.update(envelope.pop("$defs", {}))
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://codespy.local/contracts/AgentEventV1.schema.json",
        "title": "AgentEventV1",
        "protocol_version": PROTOCOL_VERSION,
        "envelope": envelope,
        "payloads": payloads,
        "transient_types": sorted(TRANSIENT_TYPES),
        "$defs": defs,
    }
