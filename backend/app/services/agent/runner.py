# ruff: noqa: E501
"""The agent loop: one model call, its tool calls, and repeat, safe to stop and start again.

Every decision and every result goes in the journal and the event log before anything depends on
it, under identifiers fixed by position (turn, step, tool call). So the loop can be killed at any
point and run again: finished model calls and tools are replayed from the journal without being
repeated, a plan that was approved is applied exactly as approved, and a command that began but
never reported is reported as ``outcome_unknown`` and is never run a second time.

The loop stops, without holding anything, whenever it needs a person (an approval) and is started
again when they answer. It stops after the current step when interrupted, for good when cancelled,
and when a budget runs out.
"""

import dataclasses
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from app.agents.prompts import load_prompt
from app.contracts.agent_events import AgentEventV1
from app.contracts.agent_tools import AgentToolResultV1
from app.db.transaction import transaction
from app.domain.scope import TenantContext
from app.ports.agent_model import (
    AgentModelPort,
    Finished,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    ModelToolSpec,
    ModelUnavailable,
    ModelUsage,
    TextDelta,
    ToolCallRequested,
)
from app.ports.sandbox import SandboxPort
from app.services.agent.approvals import ApprovalService
from app.services.agent.control import FINISHED, SessionControl, SessionRow
from app.services.agent.event_log import AgentEventLog
from app.services.agent.journal import AgentJournal
from app.services.agent.policy import PolicyV1
from app.services.agent.tools import tools_for_mode
from app.services.agent.tools.base import Tool, ToolContext, fail
from app.services.agent.tools.edit_tools import (
    EditPlan,
    EditTool,
    edit_plan_from_json,
    edit_plan_to_json,
)
from app.services.agent.tools.exec_tools import (
    ExecPlan,
    ExecTool,
    exec_plan_from_json,
    exec_plan_to_json,
)
from app.services.agent.tools.output import OutputBatch, OutputCoalescer
from app.services.agent.tools.ports import BaseFilesPort
from app.services.agent.tools.registry import ToolRegistry, guarded
from app.services.agent.tools.runners import event_payload
from app.services.agent.tools.text import redact
from app.services.workspace.service import OpenWorkspace, WorkspaceService

logger = logging.getLogger(__name__)

OutcomeKind = Literal[
    "finished", "parked", "interrupted", "cancelled", "limit", "failed", "busy", "lease_lost"
]
DELTA_MAX_CHARS = 4096
UNKNOWN_TEXT = (
    "The run was interrupted while this command was running, so it is not known whether it finished "
    "or what it changed. It was not run again. Check the workspace before relying on it."
)


@dataclass(frozen=True)
class RunOutcome:
    kind: OutcomeKind
    detail: str = ""


@dataclass
class AgentDeps:
    factory: Any
    log: AgentEventLog
    journal: AgentJournal
    control: SessionControl
    approvals: ApprovalService
    model: AgentModelPort
    sandbox: SandboxPort
    workspaces: WorkspaceService
    #: Files of the snapshot the session started from, for the git tools (optional).
    base_files: Callable[[SessionRow], Awaitable[BaseFilesPort | None]] | None = None
    policy: PolicyV1 = dataclasses.field(default_factory=PolicyV1)


@dataclass
class _Run:
    ctx: TenantContext
    row: SessionRow
    turn: int
    workspace: OpenWorkspace
    tool_ctx: ToolContext
    registry: ToolRegistry
    policy: PolicyV1


class _Parked(Exception):  # noqa: N818 -- control flow, not an error
    """The loop must wait for a person."""


class SessionRunner:
    def __init__(self, deps: AgentDeps) -> None:
        self.d = deps
        self._prompt = load_prompt("coding_agent", "v1")

    # --- entry points ----------------------------------------------------------------------------

    async def run(self, ctx: TenantContext, session_id: UUID) -> RunOutcome:
        """Drive the session until it finishes its turn, needs a person, or is stopped."""
        if not await self.d.control.acquire(ctx.org_id, session_id):
            return RunOutcome("busy")
        try:
            return await self._drive(ctx, session_id)
        finally:
            await self.d.control.release(ctx.org_id, session_id)

    async def cancel(self, ctx: TenantContext, session_id: UUID) -> RunOutcome:
        """End a session that is not running. A running one sees the flag at its next step."""
        await self.d.control.request(ctx.org_id, session_id, "cancel")
        if not await self.d.control.acquire(ctx.org_id, session_id):
            return RunOutcome("busy", "the loop will stop at its next step")
        try:
            row = await self.d.control.load(ctx.org_id, session_id)
            return await self._cancelled(ctx, row)
        finally:
            await self.d.control.release(ctx.org_id, session_id)

    # --- the loop --------------------------------------------------------------------------------

    async def _drive(self, ctx: TenantContext, session_id: UUID) -> RunOutcome:
        org = ctx.org_id
        row = await self.d.control.load(org, session_id)
        if row.status in FINISHED:
            return RunOutcome("cancelled" if row.status == "cancelled" else "finished")
        if row.control == "cancel":
            return await self._cancelled(ctx, row)
        events = await self._events(org, session_id)
        turn = sum(1 for e in events if e.type == "message.user")
        if turn == 0:
            return RunOutcome("finished", "nothing to answer")
        workspace = await self.d.workspaces.open(
            ctx, session_id, row.repository_id, row.snapshot_id
        )
        registry = tools_for_mode(row.mode)
        base = await self.d.base_files(row) if self.d.base_files else None
        run = _Run(
            ctx=ctx,
            row=row,
            turn=turn,
            workspace=workspace,
            tool_ctx=ToolContext(
                sandbox=self.d.sandbox,
                handle=workspace.handle,
                base=base,
                workspace=self.d.workspaces.bind(workspace),
            ),
            registry=registry,
            policy=self.d.policy.with_rules(await self.d.approvals.session_rules(org, session_id)),
        )
        try:
            await self._close_earlier_turns(run)
            await self.d.control.set_status(org, session_id, "running", turn=turn)
            step = 0
            while True:
                if (stop := await self._should_stop(run, step)) is not None:
                    return stop
                turn_record = await self._model_step(run, step)
                if isinstance(turn_record, RunOutcome):
                    return turn_record
                calls = turn_record["tool_calls"]
                if not calls:
                    await self._finish(run)
                    return RunOutcome("finished")
                for call in calls:
                    if (stop := await self._should_stop(run, step)) is not None:
                        return stop
                    await self._tool(
                        run, ModelToolCall(call["id"], call["name"], call["arguments_json"])
                    )
                step += 1
        except _Parked:
            return RunOutcome("parked")

    async def _should_stop(self, run: _Run, step: int) -> RunOutcome | None:
        org, sid = run.ctx.org_id, run.row.id
        if not await self.d.control.renew(org, sid):
            return RunOutcome("lease_lost")
        current = await self.d.control.load(org, sid)
        if current.control == "cancel":
            return await self._cancelled(run.ctx, current)
        if current.control == "interrupt":
            await self.d.control.clear_interrupt(org, sid)
            await self.d.control.set_status(
                org, sid, "interrupted", turn=run.turn, reason="interrupted by the user"
            )
            return RunOutcome("interrupted")
        limits = current.limits
        tools_used = await self.d.journal.count(org, sid, "tool")
        spent = sum(
            r.get("usage", {}).get("cost_micros", 0)
            for _, r in await self.d.journal.receipts(org, sid, "model")
        )
        for reason, over in (
            ("step_limit", step >= limits["max_steps"]),
            ("tool_call_limit", tools_used >= limits["max_tool_calls"]),
            ("cost_limit", spent >= limits["cost_micros_limit"]),
        ):
            if over:
                await self._emit(
                    run,
                    "error",
                    {
                        "code": reason,
                        "message": f"The session reached its {reason.replace('_', ' ')}.",
                        "retryable": False,
                        "scope": "turn",
                    },
                    key=f"error.{reason}.{run.turn}",
                )
                await self.d.control.set_status(org, sid, "idle", turn=run.turn, reason=reason)
                return RunOutcome("limit", reason)
        return None

    async def _cancelled(self, ctx: TenantContext, row: SessionRow) -> RunOutcome:
        org, sid = ctx.org_id, row.id
        await self.d.control.set_status(
            org, sid, "cancelled", turn=0, reason="cancelled by the user"
        )
        workspace = await self.d.workspaces.open(ctx, sid, row.repository_id, row.snapshot_id)
        await self.d.sandbox.release(workspace.handle)
        await self.d.workspaces.close(workspace)
        return RunOutcome("cancelled")

    async def _finish(self, run: _Run) -> None:
        await self.d.control.set_status(run.ctx.org_id, run.row.id, "idle", turn=run.turn)

    # --- model step ------------------------------------------------------------------------------

    async def _model_step(self, run: _Run, step: int) -> dict[str, Any] | RunOutcome:
        org, sid = run.ctx.org_id, run.row.id
        eid = f"model:{run.turn}:{step}"
        message_id = f"m{run.turn}.{step}"
        record = await self.d.journal.get(org, sid, eid, "receipt")
        if record is None:
            await self._emit(
                run,
                "message.started",
                {"message_id": message_id, "role": "assistant", "model_id": "agent-model"},
                key=f"message.started.{message_id}",
                parent=f"message:{message_id}",
            )
            request = await self._request(run)
            try:
                record = await self._call_model(run, request, message_id)
            except ModelUnavailable as unavailable:
                await self._emit(
                    run,
                    "error",
                    {
                        "code": unavailable.code,
                        "message": "The model could not answer. Try again.",
                        "retryable": unavailable.retryable,
                        "scope": "turn",
                    },
                    key=f"error.model.{eid}",
                )
                await self.d.control.set_status(
                    org, sid, "idle", turn=run.turn, reason=unavailable.code
                )
                return RunOutcome("failed", unavailable.code)
            await self.d.journal.put(org, sid, eid, "receipt", "model", record)
        usage = record["usage"]
        await self._emit(
            run,
            "message.completed",
            {
                "message_id": message_id,
                "text": record["text"],
                "stop_reason": record["stop_reason"],
                "usage": {
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "cache_read_tokens": usage["cache_read_tokens"],
                    "cache_write_tokens": usage["cache_write_tokens"],
                },
            },
            key=f"message.completed.{message_id}",
            parent=f"message:{message_id}",
        )
        await self._limits(run, step, eid)
        return record

    async def _call_model(
        self, run: _Run, request: ModelRequest, message_id: str
    ) -> dict[str, Any]:
        async def sink(batch: OutputBatch) -> None:
            await self._emit(
                run,
                "message.delta",
                {"message_id": message_id, "offset": batch.offset, "text": batch.text},
                key=f"message.delta.{message_id}.{batch.offset}",
                parent=f"message:{message_id}",
            )

        coalescer = OutputCoalescer(sink, max_live_bytes=256 * 1024)
        text: list[str] = []
        calls: list[ModelToolCall] = []
        usage, stop, model_id = ModelUsage(), "end_turn", "agent-model"
        try:
            async for event in self.d.model.stream(request):
                if isinstance(event, TextDelta):
                    text.append(event.text)
                    await coalescer.feed("assistant", event.text)
                elif isinstance(event, ToolCallRequested):
                    calls.append(event.call)
                elif isinstance(event, Finished):
                    usage, stop, model_id = event.usage, event.stop_reason, event.model_id
        finally:
            await coalescer.close()
        return {
            "message_id": message_id,
            "model_id": model_id,
            "text": "".join(text),
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments_json": c.arguments_json} for c in calls
            ],
            "stop_reason": stop,
            "usage": dataclasses.asdict(usage),
        }

    async def _request(self, run: _Run) -> ModelRequest:
        org, sid = run.ctx.org_id, run.row.id
        events = await self._events(org, sid)
        models = {r["message_id"]: r for _, r in await self.d.journal.receipts(org, sid, "model")}
        tools = {
            eid.removeprefix("tool:"): r
            for eid, r in await self.d.journal.receipts(org, sid, "tool")
        }
        messages: list[ModelMessage] = []
        for event in events:
            if event.type == "message.user":
                messages.append(ModelMessage("user", text=event.payload["text"]))
            elif event.type == "message.completed" and event.payload["message_id"] in models:
                record = models[event.payload["message_id"]]
                calls = tuple(
                    ModelToolCall(c["id"], c["name"], c["arguments_json"])
                    for c in record["tool_calls"]
                )
                messages.append(ModelMessage("assistant", text=record["text"], tool_calls=calls))
                for call in calls:
                    result = tools.get(call.id)
                    if result is not None:
                        messages.append(
                            ModelMessage("tool", text=_for_model(result), tool_call_id=call.id)
                        )
        specs = tuple(
            ModelToolSpec(s.name, s.description, s.parameters_schema) for s in run.registry.specs()
        )
        return ModelRequest(
            system=self._prompt.text("system.md"),
            messages=tuple(messages),
            tools=specs,
            profile=run.row.model_profile,
        )

    async def _limits(self, run: _Run, step: int, eid: str) -> None:
        org, sid = run.ctx.org_id, run.row.id
        models = await self.d.journal.receipts(org, sid, "model")
        limits = run.row.limits
        await self._emit(
            run,
            "session.limits",
            {
                "steps_used": step + 1,
                "steps_max": limits["max_steps"],
                "tool_calls_used": await self.d.journal.count(org, sid, "tool"),
                "tool_calls_max": limits["max_tool_calls"],
                "tokens_in": sum(r["usage"]["input_tokens"] for _, r in models),
                "tokens_out": sum(r["usage"]["output_tokens"] for _, r in models),
                "cost_micros_reserved": 0,
                "cost_micros_settled": sum(r["usage"]["cost_micros"] for _, r in models),
                "cost_micros_limit": limits["cost_micros_limit"],
            },
            key=f"session.limits.{eid}",
        )

    # --- one tool call ---------------------------------------------------------------------------

    async def _tool(self, run: _Run, call: ModelToolCall) -> None:
        org, sid = run.ctx.org_id, run.row.id
        eid = f"tool:{call.id}"
        phases = await self.d.journal.phases(org, sid, eid)
        if "receipt" in phases:
            await self._completed(
                run,
                call,
                AgentToolResultV1.model_validate_json(json.dumps(phases["receipt"]["result"])),
            )
            return
        parsed = run.registry.parse(call.name, call.arguments_json)
        if isinstance(parsed, AgentToolResultV1):
            await self._proposed(run, call, "read", call.arguments_json, "low")
            await self._finish_tool(run, call, parsed)
            return
        tool, args = parsed
        if isinstance(tool, EditTool):
            await self._edit(run, call, tool, args, phases)
        elif isinstance(tool, ExecTool):
            await self._exec(run, call, tool, args, phases)
        else:
            await self._read(run, call, tool, args)

    async def _read(self, run: _Run, call: ModelToolCall, tool: Tool, args: Any) -> None:
        await self._proposed(run, call, "read", call.arguments_json, "low")
        await self._emit(
            run,
            "tool.call.started",
            {"tool_call_id": call.id},
            key=f"tool.started.{call.id}",
            parent=f"tool_call:{call.id}",
        )
        result = await guarded(call.name, lambda: tool.run(run.tool_ctx, args))
        await self._finish_tool(run, call, result)

    async def _edit(
        self,
        run: _Run,
        call: ModelToolCall,
        tool: EditTool,
        args: Any,
        phases: dict[str, dict[str, Any]],
    ) -> None:
        org, sid = run.ctx.org_id, run.row.id
        eid = f"tool:{call.id}"
        intent: dict[str, Any]
        if "intent" not in phases:
            planned: EditPlan | AgentToolResultV1 = await _guard_plan(
                call.name, lambda: tool.prepare(run.tool_ctx, args)
            )
            if isinstance(planned, AgentToolResultV1):
                await self._proposed(run, call, "edit", call.arguments_json, "medium")
                await self._finish_tool(run, call, planned)
                return
            decision = run.policy.decide("edit", planned.summary, mode=run.row.mode)
            intent = {
                "tool": call.name,
                "arguments_json": call.arguments_json,
                "plan": edit_plan_to_json(planned),
                "verdict": decision.verdict,
                "reason": decision.reason,
                "rule": decision.rule,
            }
            await self.d.journal.put(org, sid, eid, "intent", "tool", intent)
            phases["intent"] = intent
            await self._proposed(run, call, "edit", planned.summary, "medium")
            base = await self.d.workspaces.head(run.workspace)
            await self._emit(
                run,
                "diff.proposed",
                {
                    "patch_id": call.id,
                    "base_revision": base,
                    "files": [
                        {
                            "path": f.path,
                            "change": f.change,
                            "before_hash": f.before_hash,
                            "after_hash": f.after_hash,
                            **({"renamed_from": f.renamed_from} if f.renamed_from else {}),
                            "hunks": [
                                h.model_dump(mode="json", exclude_none=True) for h in f.hunks[:200]
                            ],
                        }
                        for f in planned.files[:50]
                    ],
                },
                key=f"diff.proposed.{call.id}",
                parent=f"patch:{call.id}",
            )
        intent = phases["intent"]
        plan = edit_plan_from_json(intent["plan"])
        verdict = await self._authorise(run, call, "patch", intent, plan.summary)
        if verdict is not None:
            await self._emit(
                run,
                "diff.decided",
                {"patch_id": call.id, "decision": "rejected", "by": verdict.by},
                key=f"diff.decided.{call.id}",
                parent=f"patch:{call.id}",
            )
            await self._finish_tool(run, call, verdict.result)
            return
        await self._emit(
            run,
            "tool.call.started",
            {"tool_call_id": call.id},
            key=f"tool.started.{call.id}",
            parent=f"tool_call:{call.id}",
        )
        result = await guarded(
            call.name,
            lambda: tool.apply(
                run.tool_ctx, plan, request_digest=tool.request_digest(call.name, args)
            ),
        )
        applied = result.status == "ok"
        by = "policy" if intent["verdict"] == "allow" else (await self._decider(run, call))
        await self._emit(
            run,
            "diff.decided",
            {
                "patch_id": call.id,
                "decision": "applied" if applied else "rejected",
                "hunks_applied": [h.hunk_id for f in plan.files for h in f.hunks][:1000]
                if applied
                else [],
                "by": by,
            },
            key=f"diff.decided.{call.id}",
            parent=f"patch:{call.id}",
        )
        await self._finish_tool(run, call, result)

    async def _exec(
        self,
        run: _Run,
        call: ModelToolCall,
        tool: ExecTool,
        args: Any,
        phases: dict[str, dict[str, Any]],
    ) -> None:
        org, sid = run.ctx.org_id, run.row.id
        eid = f"tool:{call.id}"
        intent: dict[str, Any]
        if "started" in phases:  # it began and never reported: do not run it again
            await self._proposed_replay(run, call, phases)
            changed = await run.tool_ctx.workspace.sync() if run.tool_ctx.workspace else None
            note = (
                f" Files changed before it stopped were recorded (workspace revision {changed.revision})."
                if changed
                else ""
            )
            unknown = AgentToolResultV1(
                status="outcome_unknown",
                summary="outcome unknown: the run was interrupted",
                content=UNKNOWN_TEXT + note,
                bytes_total=len(UNKNOWN_TEXT),
                error_code="outcome_unknown",
            )
            await self._finish_tool(run, call, unknown)
            return
        if "intent" not in phases:
            planned: ExecPlan | AgentToolResultV1 = await _guard_plan(
                call.name, lambda: tool.prepare(run.tool_ctx, args)
            )
            if isinstance(planned, AgentToolResultV1):
                await self._proposed(run, call, "exec", call.arguments_json, "medium")
                await self._finish_tool(run, call, planned)
                return
            decision = run.policy.decide("exec", planned.rule_key, mode=run.row.mode)
            intent = {
                "tool": call.name,
                "arguments_json": call.arguments_json,
                "plan": exec_plan_to_json(planned),
                "verdict": decision.verdict,
                "reason": decision.reason,
                "rule": decision.rule,
            }
            await self.d.journal.put(org, sid, eid, "intent", "tool", intent)
            phases["intent"] = intent
            await self._proposed(run, call, "exec", planned.display, planned.risk)
        intent = phases["intent"]
        plan = exec_plan_from_json(intent["plan"])
        verdict = await self._authorise(run, call, "tool_call", intent, plan.display)
        if verdict is not None:
            await self._finish_tool(run, call, verdict.result)
            return
        await self.d.journal.put(org, sid, eid, "started", "tool", {"at": "approved"})
        await self._emit(
            run,
            "tool.call.started",
            {"tool_call_id": call.id},
            key=f"tool.started.{call.id}",
            parent=f"tool_call:{call.id}",
        )

        async def sink(batch: OutputBatch) -> None:
            await self._emit(
                run,
                "tool.call.output",
                {
                    "tool_call_id": call.id,
                    "stream": batch.stream,
                    "offset": batch.offset,
                    "text": batch.text,
                },
                key=f"tool.output.{call.id}.{batch.stream}.{batch.offset}",
                parent=f"tool_call:{call.id}",
            )

        streaming = dataclasses.replace(run.tool_ctx, on_output=sink)
        result = await guarded(call.name, lambda: tool.run_plan(streaming, plan))
        await self._finish_tool(run, call, result)

    # --- approvals -------------------------------------------------------------------------------

    @dataclass
    class _Refused:
        result: AgentToolResultV1
        by: str

    async def _authorise(
        self, run: _Run, call: ModelToolCall, kind: str, intent: dict[str, Any], what: str
    ) -> "SessionRunner._Refused | None":
        """``None`` if the tool may proceed; a refusal (denied by policy, the person, or expiry) otherwise.

        Raises ``_Parked`` when the person has yet to answer.
        """
        org, sid = run.ctx.org_id, run.row.id
        if intent["verdict"] == "allow":
            return None
        if intent["verdict"] == "deny":
            return self._Refused(
                fail("denied", "policy_denied", f"Not allowed: {intent['reason']}."), "policy"
            )
        approval_id = f"appr-{call.id}"
        row = await self.d.approvals.request(
            org,
            sid,
            approval_id,
            kind,
            call.id,
            reason=intent["reason"],
            rule_matched=intent.get("rule"),
            turn=run.turn,
        )
        row = await self.d.approvals.expire_if_due(org, sid, approval_id, run.turn)
        if row.status == "pending":
            await self.d.control.set_status(org, sid, "waiting_approval", turn=run.turn)
            raise _Parked
        await self.d.control.set_status(org, sid, "running", turn=run.turn)
        if row.status == "approved":
            return None
        why = (
            "The person did not answer in time."
            if row.status == "expired"
            else "The person rejected this request."
        )
        return self._Refused(
            fail(
                "denied",
                "rejected_by_user" if row.status == "rejected" else "approval_expired",
                f"{why} It was not done. Do not try another way to get the same result; say what you could not do.",
            ),
            row.decided_by or "system",
        )

    async def _decider(self, run: _Run, call: ModelToolCall) -> str:
        row = await self.d.approvals.get(run.ctx.org_id, run.row.id, f"appr-{call.id}")
        return str((row.decided_by if row else None) or "user")

    # --- events and receipts ---------------------------------------------------------------------

    async def _proposed(
        self, run: _Run, call: ModelToolCall, tool_class: str, preview: str, risk: str
    ) -> None:
        text, _ = redact(preview)
        await self._emit(
            run,
            "tool.call.proposed",
            {
                "tool_call_id": call.id,
                "tool": call.name,
                "class": tool_class,
                "args_preview": text[:2000],
                "risk": risk,
            },
            key=f"tool.proposed.{call.id}",
            parent=f"tool_call:{call.id}",
        )

    async def _proposed_replay(
        self, run: _Run, call: ModelToolCall, phases: dict[str, dict[str, Any]]
    ) -> None:
        plan = phases.get("intent", {}).get("plan", {})
        await self._proposed(
            run, call, "exec", plan.get("display", call.arguments_json), plan.get("risk", "medium")
        )

    async def _finish_tool(self, run: _Run, call: ModelToolCall, result: AgentToolResultV1) -> None:
        org, sid = run.ctx.org_id, run.row.id
        await self.d.journal.put(
            org,
            sid,
            f"tool:{call.id}",
            "receipt",
            "tool",
            {"result": result.model_dump(mode="json")},
        )
        await self._completed(run, call, result)
        await self._limits(run, 0, f"tool:{call.id}")

    async def _completed(self, run: _Run, call: ModelToolCall, result: AgentToolResultV1) -> None:
        await self._emit(
            run,
            "tool.call.completed",
            {
                "tool_call_id": call.id,
                "status": result.status,
                "summary": result.summary[:1024],
                "truncated": result.truncated,
                "bytes": result.bytes_total,
                "redactions": result.redactions,
                "content_ref": f"tool:{call.id}"[:128],
                "evidence": [e.model_dump(mode="json", exclude_none=True) for e in result.evidence],
            },
            key=f"tool.completed.{call.id}",
            parent=f"tool_call:{call.id}",
        )
        if result.tests is not None:
            await self._emit(
                run,
                "test.result",
                event_payload(call.id, result.tests),
                key=f"test.result.{call.id}",
            )

    async def _emit(
        self, run: _Run, kind: str, payload: dict[str, Any], *, key: str, parent: str | None = None
    ) -> AgentEventV1:
        async with transaction(self.d.factory) as s:
            return await self.d.log.append(
                s,
                run.ctx.org_id,
                run.row.id,
                kind,
                payload,
                turn=run.turn,
                parent=parent,
                event_key=key,
            )

    async def _events(self, org: UUID, session: UUID) -> list[AgentEventV1]:
        out: list[AgentEventV1] = []
        after = 0
        async with transaction(self.d.factory) as s:
            while True:
                page = await self.d.log.read_after(s, org, session, after, 200)
                out.extend(page)
                if len(page) < 200:
                    return out
                after = page[-1].seq

    async def _close_earlier_turns(self, run: _Run) -> None:
        """Answer tool calls an earlier, abandoned turn left open, so the model sees a whole conversation."""
        org, sid = run.ctx.org_id, run.row.id
        for turn in range(1, run.turn):
            step = 0
            while (
                record := await self.d.journal.get(org, sid, f"model:{turn}:{step}", "receipt")
            ) is not None:
                for c in record["tool_calls"]:
                    phases = await self.d.journal.phases(org, sid, f"tool:{c['id']}")
                    if "receipt" in phases:
                        continue
                    started = "started" in phases
                    result = AgentToolResultV1(
                        status="outcome_unknown" if started else "denied",
                        summary="not completed: the turn was interrupted",
                        content=UNKNOWN_TEXT
                        if started
                        else "This call was not run: the turn it belonged to was interrupted.",
                        error_code="outcome_unknown" if started else "interrupted",
                    )
                    await self.d.journal.put(
                        org,
                        sid,
                        f"tool:{c['id']}",
                        "receipt",
                        "tool",
                        {"result": result.model_dump(mode="json")},
                    )
                    old = dataclasses.replace(run, turn=turn)
                    await self._completed(
                        old, ModelToolCall(c["id"], c["name"], c["arguments_json"]), result
                    )
                step += 1


async def _guard_plan(name: str, work: Callable[[], Awaitable[Any]]) -> Any:
    """Plan a tool call; a failure to plan is a result the model reads, not an exception."""
    holder: dict[str, Any] = {}

    async def run() -> AgentToolResultV1:
        holder["plan"] = await work()
        return AgentToolResultV1(status="ok")

    outcome = await guarded(name, run)
    return holder["plan"] if outcome.status == "ok" and "plan" in holder else outcome


def _for_model(record: dict[str, Any]) -> str:
    """A tool's result as the model reads it: the content, led by the status when it is not plain ok."""
    result = record["result"]
    status = result["status"]
    head = "" if status == "ok" else f"[{status}] "
    return head + str(result["content"])
