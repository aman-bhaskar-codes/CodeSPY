"""Fold an event sequence into the compacted state served by ``GET /state``.

The fold is pure and idempotent over duplicate and out-of-order deltas: a client that reconnects
and replays events must reach the same state as one that never disconnected. ``message.completed``
is authoritative for message text.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.contracts.agent_events import AgentEventV1, SessionLimits
from app.contracts.agent_session import (
    AgentApprovalViewV1,
    AgentMessageViewV1,
    AgentStateV1,
    AgentToolSummaryV1,
)


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _slice_utf16(text: str, start: int) -> str:
    return text.encode("utf-16-le")[start * 2 :].decode("utf-16-le", errors="ignore")


@dataclass
class _Message:
    role: str
    text: str = ""
    completed: bool = False


@dataclass
class _Tool:
    tool: str
    status: str = "proposed"
    summary: str = ""


@dataclass
class _Approval:
    kind: str
    ref: str
    status: str = "pending"


@dataclass
class _State:
    session_id: str
    as_of_seq: int = 0
    status: str = "idle"
    mode: str = "plan"
    turn: int = 0
    limits: dict[str, Any] | None = None
    messages: dict[str, _Message] = field(default_factory=dict)
    tools: dict[str, _Tool] = field(default_factory=dict)
    approvals: dict[str, _Approval] = field(default_factory=dict)
    checkpoints: list[str] = field(default_factory=list)
    last_error_code: str | None = None


def reduce_events(session_id: UUID, events: Iterable[AgentEventV1]) -> AgentStateV1:
    state = _State(str(session_id))
    for event in events:
        if event.seq <= state.as_of_seq:
            continue  # duplicate or replayed event
        state.as_of_seq = event.seq
        state.turn = max(state.turn, event.turn)
        _apply(state, event.type, event.payload)
    return AgentStateV1(
        session_id=session_id,
        as_of_seq=state.as_of_seq,
        status=state.status,  # type: ignore[arg-type]
        mode=state.mode,  # type: ignore[arg-type]
        turn=state.turn,
        limits=SessionLimits.model_validate_json(_dump(state.limits)) if state.limits else None,
        messages=tuple(
            AgentMessageViewV1(message_id=k, role=m.role, text=m.text, completed=m.completed)  # type: ignore[arg-type]
            for k, m in state.messages.items()
        ),
        tools=tuple(
            AgentToolSummaryV1(tool_call_id=k, tool=t.tool, status=t.status, summary=t.summary)  # type: ignore[arg-type]
            for k, t in state.tools.items()
        ),
        approvals=tuple(
            AgentApprovalViewV1(
                approval_id=k,
                subject_kind=a.kind,
                subject_ref=a.ref,
                status=a.status,  # type: ignore[arg-type]
            )
            for k, a in state.approvals.items()
        ),
        checkpoints=tuple(state.checkpoints),
        last_error_code=state.last_error_code,
    )


def _dump(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value)


def _apply(state: _State, kind: str, p: dict[str, Any]) -> None:
    if kind == "session.created":
        state.mode = p["mode"]
        state.status = "idle"
    elif kind == "session.status":
        state.status = p["status"]
    elif kind == "session.mode.changed":
        state.mode = p["mode"]
    elif kind == "session.limits":
        state.limits = p
    elif kind == "message.user":
        state.messages[p["message_id"]] = _Message("user", p["text"], True)
    elif kind == "message.started":
        state.messages.setdefault(p["message_id"], _Message("assistant"))
    elif kind == "message.delta":
        message = state.messages.setdefault(p["message_id"], _Message("assistant"))
        if message.completed:
            return
        have = _utf16_len(message.text)
        offset = p["offset"]
        if offset > have:
            return  # a gap; message.completed will carry the full text
        message.text += _slice_utf16(p["text"], have - offset)
    elif kind == "message.completed":
        message = state.messages.setdefault(p["message_id"], _Message("assistant"))
        message.text, message.completed = p["text"], True
    elif kind == "tool.call.proposed":
        state.tools.setdefault(p["tool_call_id"], _Tool(p["tool"]))
    elif kind == "tool.call.started":
        if (tool := state.tools.get(p["tool_call_id"])) is not None:
            tool.status = "running"
    elif kind == "tool.call.completed":
        tool = state.tools.setdefault(p["tool_call_id"], _Tool("unknown"))
        tool.status, tool.summary = p["status"], p.get("summary", "")
    elif kind == "approval.requested":
        subject = p["subject"]
        state.approvals[p["approval_id"]] = _Approval(subject["kind"], subject["ref"])
    elif kind == "approval.resolved":
        if (approval := state.approvals.get(p["approval_id"])) is not None:
            approval.status = p["decision"]
    elif kind == "checkpoint.created":
        if p["checkpoint_id"] not in state.checkpoints:
            state.checkpoints.append(p["checkpoint_id"])
    elif kind == "error":
        state.last_error_code = p["code"]
