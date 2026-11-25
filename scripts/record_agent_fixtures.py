"""Record the three canonical agent sessions used by the frontend and backend tests.

Events are built through the backend contracts, so every payload is canonical and valid, and written
with deterministic timestamps. Run after changing the protocol: ``uv run python scripts/record_agent_fixtures.py``.
"""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.contracts.agent_events import AgentEventV1, validate_payload

TARGET = ROOT / "frontend/src/features/agent/fixtures"
BASE = datetime(2026, 11, 25, 10, 0, 0, tzinfo=UTC)
SHA_A, SHA_B = "a" * 64, "b" * 64


class Recording:
    def __init__(self, session_id: str, name: str, description: str) -> None:
        self.session_id, self.name, self.description = session_id, name, description
        self.events: list[AgentEventV1] = []

    def add(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        turn: int = 1,
        parent: str | None = None,
    ) -> None:
        seq = len(self.events) + 1
        ts = (BASE + timedelta(milliseconds=250 * seq)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
        self.events.append(
            AgentEventV1(
                session_id=self.session_id,
                seq=seq,
                type=kind,
                ts=ts,
                turn=turn,
                parent=parent,
                payload=validate_payload(kind, payload),
            )
        )

    def document(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "session_id": self.session_id,
            "events": [
                json.loads(e.model_dump_json(exclude_none=True)) for e in self.events
            ],
        }


def created(r: Recording, mode: str = "act") -> None:
    r.add(
        "session.created",
        {
            "mode": mode,
            "base_snapshot_id": "30000000-0000-4000-8000-000000000001",
            "model_profile": "agent",
            "limits": {
                "max_steps": 60,
                "max_tool_calls": 150,
                "cost_micros_limit": 5_000_000,
            },
        },
        turn=0,
    )


def user(r: Recording, text: str) -> None:
    r.add(
        "message.user",
        {
            "message_id": "m1",
            "text": text,
            "context": [
                {"kind": "file", "ref": "backend/app/auth.py", "start_line": 1}
            ],
        },
    )


def happy_path() -> Recording:
    r = Recording(
        "10000000-0000-4000-8000-000000000001",
        "happy-path",
        "Read a file, propose a patch, approve it, apply it, run the tests, and finish.",
    )
    created(r)
    user(r, "Fix the failing test in auth.py")
    r.add("session.status", {"status": "running"})
    r.add(
        "plan.proposed",
        {
            "plan_id": "p1",
            "steps": [
                {"id": "s1", "title": "Read the failing test and auth.py"},
                {"id": "s2", "title": "Fix the token expiry check"},
                {"id": "s3", "title": "Run the tests"},
            ],
        },
    )
    r.add(
        "message.started",
        {"message_id": "m2", "model_id": "agent-v1"},
        parent="message:m2",
    )
    r.add(
        "message.delta",
        {"message_id": "m2", "offset": 0, "text": "I'll read the test "},
        parent="message:m2",
    )
    r.add(
        "message.delta",
        {"message_id": "m2", "offset": 19, "text": "and the code it covers."},
        parent="message:m2",
    )
    r.add(
        "message.completed",
        {
            "message_id": "m2",
            "text": "I'll read the test and the code it covers.",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1200, "output_tokens": 18},
        },
        parent="message:m2",
    )
    r.add(
        "tool.call.proposed",
        {
            "tool_call_id": "tc1",
            "tool": "read_file",
            "class": "read",
            "args_preview": "backend/app/auth.py 1-120",
        },
        parent="tool_call:tc1",
    )
    r.add("tool.call.started", {"tool_call_id": "tc1"}, parent="tool_call:tc1")
    r.add(
        "tool.call.completed",
        {
            "tool_call_id": "tc1",
            "status": "ok",
            "summary": "read 120 lines",
            "bytes": 4210,
            "evidence": [
                {
                    "evidence_id": "e1",
                    "provenance": {
                        "kind": "source_range",
                        "path": "backend/app/auth.py",
                        "start_line": 1,
                        "end_line": 120,
                    },
                }
            ],
        },
        parent="tool_call:tc1",
    )
    r.add(
        "plan.updated",
        {
            "plan_id": "p1",
            "steps": [
                {
                    "id": "s1",
                    "title": "Read the failing test and auth.py",
                    "status": "completed",
                },
                {
                    "id": "s2",
                    "title": "Fix the token expiry check",
                    "status": "in_progress",
                },
                {"id": "s3", "title": "Run the tests"},
            ],
        },
    )
    r.add(
        "diff.proposed",
        {
            "patch_id": "patch1",
            "base_revision": 0,
            "files": [
                {
                    "path": "backend/app/auth.py",
                    "change": "modify",
                    "before_hash": SHA_A,
                    "after_hash": SHA_B,
                    "hunks": [
                        {
                            "hunk_id": "h1",
                            "old_start": 58,
                            "old_lines": 1,
                            "new_start": 58,
                            "new_lines": 1,
                            "preview": "-    if now > exp:\n+    if now >= exp:",
                        }
                    ],
                }
            ],
        },
        parent="patch:patch1",
    )
    r.add(
        "approval.requested",
        {
            "approval_id": "a1",
            "subject": {"kind": "patch", "ref": "patch1"},
            "reason": "edits need approval",
            "expires_at": "2026-11-25T10:30:00.000Z",
        },
    )
    r.add("session.status", {"status": "waiting_approval"})
    r.add(
        "approval.resolved",
        {"approval_id": "a1", "decision": "approved", "scope": "once", "by": "user"},
    )
    r.add(
        "diff.decided",
        {
            "patch_id": "patch1",
            "decision": "applied",
            "hunks_applied": ["h1"],
            "by": "user",
        },
        parent="patch:patch1",
    )
    r.add(
        "workspace.revision",
        {
            "revision": 1,
            "changed_paths": ["backend/app/auth.py"],
            "author": "agent",
            "checkpoint_id": "cp1",
        },
    )
    r.add(
        "checkpoint.created",
        {"checkpoint_id": "cp1", "revision": 1, "label": "fix expiry", "turn": 1},
    )
    r.add("session.status", {"status": "running"})
    r.add(
        "tool.call.proposed",
        {
            "tool_call_id": "tc2",
            "tool": "run_tests",
            "class": "exec",
            "args_preview": "pytest backend/tests/test_auth.py",
            "risk": "medium",
        },
        parent="tool_call:tc2",
    )
    r.add(
        "approval.requested",
        {
            "approval_id": "a2",
            "subject": {"kind": "tool_call", "ref": "tc2"},
            "reason": "commands need approval",
            "expires_at": "2026-11-25T10:30:00.000Z",
        },
    )
    r.add(
        "approval.resolved",
        {
            "approval_id": "a2",
            "decision": "approved",
            "scope": "session_rule",
            "rule": "pytest*",
            "by": "user",
        },
    )
    r.add("tool.call.started", {"tool_call_id": "tc2"}, parent="tool_call:tc2")
    r.add(
        "tool.call.output",
        {
            "tool_call_id": "tc2",
            "stream": "stdout",
            "offset": 0,
            "text": "collected 12 items\n",
        },
        parent="tool_call:tc2",
    )
    r.add(
        "tool.call.output",
        {
            "tool_call_id": "tc2",
            "stream": "stdout",
            "offset": 19,
            "text": "12 passed in 0.8s\n",
        },
        parent="tool_call:tc2",
    )
    r.add(
        "tool.call.completed",
        {
            "tool_call_id": "tc2",
            "status": "ok",
            "summary": "exit 0 in 0.9s",
            "bytes": 37,
        },
        parent="tool_call:tc2",
    )
    r.add(
        "test.result",
        {
            "tool_call_id": "tc2",
            "runner": "pytest",
            "passed": 12,
            "failed": 0,
            "skipped": 0,
        },
    )
    r.add(
        "evidence.claim",
        {
            "claim_id": "c1",
            "kind": "observation",
            "text": "All 12 tests in test_auth.py pass after the change.",
            "provenance": [{"kind": "tool_result", "tool_call_id": "tc2"}],
            "verifier": "verified",
        },
    )
    r.add(
        "message.started",
        {"message_id": "m3", "model_id": "agent-v1"},
        parent="message:m3",
    )
    r.add(
        "message.completed",
        {
            "message_id": "m3",
            "text": "Fixed: the expiry check used `>` instead of `>=`. All 12 tests pass.",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3400, "output_tokens": 31},
            "claims": [{"claim_id": "c1"}],
        },
        parent="message:m3",
    )
    r.add(
        "session.limits",
        {
            "steps_used": 3,
            "steps_max": 60,
            "tool_calls_used": 2,
            "tool_calls_max": 150,
            "tokens_in": 4600,
            "tokens_out": 49,
            "cost_micros_reserved": 0,
            "cost_micros_settled": 21000,
            "cost_micros_limit": 5000000,
        },
    )
    r.add("session.status", {"status": "completed"})
    return r


def rejection() -> Recording:
    r = Recording(
        "10000000-0000-4000-8000-000000000002",
        "rejection",
        "A risky command and a patch are both rejected; the agent adapts and the session stays usable.",
    )
    created(r)
    user(r, "Clean the build directory and update the version")
    r.add("session.status", {"status": "running"})
    r.add(
        "tool.call.proposed",
        {
            "tool_call_id": "tc1",
            "tool": "run_command",
            "class": "exec",
            "args_preview": "rm -rf build dist",
            "risk": "high",
        },
        parent="tool_call:tc1",
    )
    r.add(
        "approval.requested",
        {
            "approval_id": "a1",
            "subject": {"kind": "tool_call", "ref": "tc1"},
            "reason": "deletes files",
            "rule_matched": "exec: rm*",
            "expires_at": "2026-11-25T10:30:00.000Z",
        },
    )
    r.add("session.status", {"status": "waiting_approval"})
    r.add(
        "approval.resolved",
        {"approval_id": "a1", "decision": "rejected", "scope": "once", "by": "user"},
    )
    r.add(
        "tool.call.completed",
        {"tool_call_id": "tc1", "status": "denied", "summary": "rejected by the user"},
        parent="tool_call:tc1",
    )
    r.add("session.status", {"status": "running"})
    r.add(
        "diff.proposed",
        {
            "patch_id": "patch1",
            "base_revision": 0,
            "files": [
                {
                    "path": "pyproject.toml",
                    "change": "modify",
                    "before_hash": SHA_A,
                    "after_hash": SHA_B,
                    "hunks": [
                        {
                            "hunk_id": "h1",
                            "old_start": 3,
                            "old_lines": 1,
                            "new_start": 3,
                            "new_lines": 1,
                            "preview": '-version = "1.0.0"\n+version = "1.0.1"',
                        }
                    ],
                }
            ],
        },
        parent="patch:patch1",
    )
    r.add(
        "approval.requested",
        {
            "approval_id": "a2",
            "subject": {"kind": "patch", "ref": "patch1"},
            "reason": "edits need approval",
            "expires_at": "2026-11-25T10:30:00.000Z",
        },
    )
    r.add("session.status", {"status": "waiting_approval"})
    r.add(
        "approval.resolved",
        {"approval_id": "a2", "decision": "rejected", "scope": "once", "by": "user"},
    )
    r.add(
        "diff.decided",
        {"patch_id": "patch1", "decision": "rejected", "by": "user"},
        parent="patch:patch1",
    )
    r.add(
        "message.started",
        {"message_id": "m2", "model_id": "agent-v1"},
        parent="message:m2",
    )
    r.add(
        "message.completed",
        {
            "message_id": "m2",
            "text": "Understood. I did not delete anything or change the version. Tell me what to do instead.",
            "stop_reason": "end_turn",
        },
        parent="message:m2",
    )
    r.add("session.status", {"status": "idle"})
    return r


def crash_resume() -> Recording:
    r = Recording(
        "10000000-0000-4000-8000-000000000003",
        "crash-resume",
        "The worker is lost while a command runs; on resume the command is reported as outcome unknown, never re-run.",
    )
    created(r)
    user(r, "Run the migration check")
    r.add("session.status", {"status": "running"})
    r.add(
        "tool.call.proposed",
        {
            "tool_call_id": "tc1",
            "tool": "run_command",
            "class": "exec",
            "args_preview": "python manage.py migrate --check",
            "risk": "medium",
        },
        parent="tool_call:tc1",
    )
    r.add(
        "approval.requested",
        {
            "approval_id": "a1",
            "subject": {"kind": "tool_call", "ref": "tc1"},
            "expires_at": "2026-11-25T10:30:00.000Z",
        },
    )
    r.add(
        "approval.resolved",
        {"approval_id": "a1", "decision": "approved", "scope": "once", "by": "user"},
    )
    r.add("tool.call.started", {"tool_call_id": "tc1"}, parent="tool_call:tc1")
    r.add(
        "tool.call.output",
        {
            "tool_call_id": "tc1",
            "stream": "stdout",
            "offset": 0,
            "text": "Checking migrations...\n",
        },
        parent="tool_call:tc1",
    )
    r.add("session.status", {"status": "interrupted", "reason": "worker_lost"})
    r.add(
        "error",
        {
            "code": "worker_lost",
            "message": "The worker stopped while a command was running.",
            "retryable": True,
            "scope": "turn",
        },
    )
    r.add("session.status", {"status": "running"})
    r.add(
        "tool.call.completed",
        {
            "tool_call_id": "tc1",
            "status": "outcome_unknown",
            "summary": "worker restarted; the command was not re-run",
        },
        parent="tool_call:tc1",
    )
    r.add(
        "message.started",
        {"message_id": "m2", "model_id": "agent-v1"},
        parent="message:m2",
    )
    r.add(
        "message.completed",
        {
            "message_id": "m2",
            "text": "The migration check was interrupted and I cannot tell whether it finished. Run it again?",
            "stop_reason": "end_turn",
        },
        parent="message:m2",
    )
    r.add("session.status", {"status": "idle"})
    return r


def documents() -> dict[str, dict[str, Any]]:
    return {r.name: r.document() for r in (happy_path(), rejection(), crash_resume())}


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    for name, document in documents().items():
        (TARGET / f"{name}.json").write_text(render(document))


if __name__ == "__main__":
    main()
