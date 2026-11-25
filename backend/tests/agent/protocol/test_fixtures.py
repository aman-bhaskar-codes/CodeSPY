"""The recorded sessions the frontend replays: valid, gap-free, and reducing as described."""

import json
import sys
from pathlib import Path
from uuid import UUID

import pytest
from app.contracts.agent_events import AgentEventV1, validate_payload
from app.services.agent.state import reduce_events

ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / "frontend/src/features/agent/fixtures"
sys.path.insert(0, str(ROOT / "scripts"))
import record_agent_fixtures as recorder  # noqa: E402

NAMES = ("happy-path", "rejection", "crash-resume")


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize("name", NAMES)
def test_fixtures_are_current_valid_and_gap_free(name):
    document = load(name)
    assert (FIXTURES / f"{name}.json").read_text() == recorder.render(recorder.documents()[name]), (
        "run: uv run python scripts/record_agent_fixtures.py"
    )
    events = [AgentEventV1.model_validate_json(json.dumps(e)) for e in document["events"]]
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert all(e.session_id == document["session_id"] for e in events)
    for e in events:  # canonical: validating again changes nothing
        assert validate_payload(e.type, e.payload) == e.payload


def reduced(name):
    document = load(name)
    events = [AgentEventV1.model_validate_json(json.dumps(e)) for e in document["events"]]
    return reduce_events(UUID(document["session_id"]), events)


def test_happy_path_reaches_a_completed_session_with_one_checkpoint():
    state = reduced("happy-path")
    assert state.status == "completed" and state.checkpoints == ("cp1",)
    assert [t.status for t in state.tools] == ["ok", "ok"]
    assert [a.status for a in state.approvals] == ["approved", "approved"]
    assert (
        state.messages[-1].completed and state.limits and state.limits.cost_micros_settled == 21000
    )


def test_rejection_keeps_the_session_usable_and_marks_the_tool_denied():
    state = reduced("rejection")
    assert state.status == "idle" and [t.status for t in state.tools] == ["denied"]
    assert [a.status for a in state.approvals] == ["rejected", "rejected"]


def test_crash_resume_reports_outcome_unknown_and_never_reruns():
    document = load("crash-resume")
    types = [e["type"] for e in document["events"]]
    assert types.count("tool.call.started") == 1  # the command is not started a second time
    state = reduced("crash-resume")
    assert [t.status for t in state.tools] == ["outcome_unknown"]
    assert state.last_error_code == "worker_lost" and state.status == "idle"
