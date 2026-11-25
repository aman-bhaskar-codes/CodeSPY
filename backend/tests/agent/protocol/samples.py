"""One valid payload per event type, shared by the contract and storage tests."""

from typing import Any

SHA = "a" * 64
HEX16 = "0123456789abcdef"

SAMPLES: dict[str, dict[str, Any]] = {
    "session.created": {
        "mode": "plan",
        "base_snapshot_id": "snap_1",
        "model_profile": "agent",
        "limits": {"max_steps": 60, "max_tool_calls": 150, "cost_micros_limit": 5000000},
    },
    "session.status": {"status": "running"},
    "session.mode.changed": {"mode": "act", "by": "user"},
    "session.limits": {
        "steps_used": 1,
        "steps_max": 60,
        "tool_calls_used": 1,
        "tool_calls_max": 150,
        "tokens_in": 100,
        "tokens_out": 20,
        "cost_micros_reserved": 10,
        "cost_micros_settled": 5,
        "cost_micros_limit": 5000000,
    },
    "message.user": {
        "message_id": "m1",
        "text": "fix the failing test",
        "context": [{"kind": "file", "ref": "src/app.py", "start_line": 1, "end_line": 20}],
    },
    "message.started": {"message_id": "m2", "role": "assistant", "model_id": "agent-v1"},
    "message.delta": {"message_id": "m2", "offset": 0, "text": "Looking"},
    "message.completed": {
        "message_id": "m2",
        "text": "Looking at it.",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 4},
        "claims": [{"claim_id": "c1"}],
    },
    "thinking.summary": {"message_id": "m2", "text": "Checked the test and its fixture."},
    "plan.proposed": {
        "plan_id": "p1",
        "steps": [{"id": "s1", "title": "Read the test", "status": "pending"}],
        "requires_approval": True,
    },
    "plan.updated": {
        "plan_id": "p1",
        "steps": [{"id": "s1", "title": "Read the test", "status": "completed"}],
    },
    "plan.decided": {"plan_id": "p1", "decision": "approved", "by": "user"},
    "tool.call.proposed": {
        "tool_call_id": "tc1",
        "tool": "read_file",
        "class": "read",
        "args_preview": "src/app.py",
        "risk": "low",
    },
    "tool.call.started": {"tool_call_id": "tc1"},
    "tool.call.output": {"tool_call_id": "tc1", "stream": "stdout", "offset": 0, "text": "ok"},
    "tool.call.completed": {
        "tool_call_id": "tc1",
        "status": "ok",
        "summary": "read 40 lines",
        "bytes": 1200,
        "evidence": [
            {
                "evidence_id": "e1",
                "provenance": {
                    "kind": "source_range",
                    "path": "src/app.py",
                    "start_line": 1,
                    "end_line": 40,
                },
            }
        ],
    },
    "approval.requested": {
        "approval_id": "a1",
        "subject": {"kind": "patch", "ref": "patch1"},
        "reason": "edit requires approval",
        "expires_at": "2026-11-25T12:00:00.000Z",
    },
    "approval.resolved": {
        "approval_id": "a1",
        "decision": "approved",
        "scope": "once",
        "by": "user",
    },
    "diff.proposed": {
        "patch_id": "patch1",
        "base_revision": 0,
        "files": [
            {
                "path": "src/app.py",
                "change": "modify",
                "before_hash": SHA,
                "after_hash": "b" * 64,
                "hunks": [
                    {
                        "hunk_id": "h1",
                        "old_start": 3,
                        "old_lines": 2,
                        "new_start": 3,
                        "new_lines": 3,
                        "preview": "-a\n+b",
                    }
                ],
            }
        ],
    },
    "diff.decided": {
        "patch_id": "patch1",
        "decision": "applied",
        "hunks_applied": ["h1"],
        "by": "user",
    },
    "workspace.revision": {
        "revision": 1,
        "changed_paths": ["src/app.py"],
        "author": "agent",
        "checkpoint_id": "cp1",
    },
    "workspace.conflict": {
        "path": "src/app.py",
        "expected_hash": SHA,
        "actual_hash": "b" * 64,
        "author_of_actual": "user",
    },
    "checkpoint.created": {"checkpoint_id": "cp1", "revision": 1, "label": "after edit", "turn": 1},
    "checkpoint.restored": {"checkpoint_id": "cp1", "restore": "both"},
    "terminal.opened": {"terminal_id": "t1", "owner": "agent"},
    "terminal.closed": {"terminal_id": "t1", "owner": "agent", "exit_code": 0},
    "test.result": {
        "tool_call_id": "tc2",
        "runner": "pytest",
        "passed": 3,
        "failed": 1,
        "skipped": 0,
        "failures": [{"name": "test_x", "file": "tests/test_x.py", "line": 9, "message": "boom"}],
    },
    "evidence.claim": {
        "claim_id": "c1",
        "kind": "observation",
        "text": "app.py defines main",
        "provenance": [
            {"kind": "report_fact", "report_id": "r1", "statement_id": HEX16},
            {"kind": "byte_range", "artifact_id": "bin1", "start_offset": "10", "end_offset": "ff"},
            {"kind": "tool_result", "tool_call_id": "tc1"},
        ],
        "verifier": "verified",
    },
    "error": {
        "code": "model_unavailable",
        "message": "The model is unavailable.",
        "retryable": True,
    },
}

# stream.reset is transient: validated, delivered to one connection, never stored.
TRANSIENT_SAMPLE = {"reason": "replay_limit"}

# A recorded session used for replay tests: (type, turn, parent).
RECORDING: list[tuple[str, int, str | None]] = [
    ("session.created", 0, None),
    ("message.user", 1, None),
    ("session.status", 1, None),
    ("message.started", 1, "message:m2"),
    ("message.delta", 1, "message:m2"),
    ("message.completed", 1, "message:m2"),
    ("tool.call.proposed", 1, "tool_call:tc1"),
    ("tool.call.started", 1, "tool_call:tc1"),
    ("tool.call.output", 1, "tool_call:tc1"),
    ("tool.call.completed", 1, "tool_call:tc1"),
    ("approval.requested", 1, None),
    ("approval.resolved", 1, None),
    ("checkpoint.created", 1, None),
    ("error", 1, None),
]
