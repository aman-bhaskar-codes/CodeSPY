# ruff: noqa: E501
"""The state fold is idempotent over duplicates and gaps, and a resumed client reaches the same state."""

from uuid import UUID

import pytest
from app.contracts.agent_events import AgentEventV1, validate_payload
from app.services.agent.state import reduce_events

from tests.agent.protocol.samples import RECORDING, SAMPLES

SESSION = UUID("10000000-0000-4000-8000-000000000001")


def recorded() -> list[AgentEventV1]:
    return [
        AgentEventV1(
            session_id=str(SESSION),
            seq=i,
            type=kind,
            ts="2026-11-25T11:24:03.120Z",
            turn=turn,
            parent=parent,
            payload=validate_payload(kind, SAMPLES[kind]),
        )
        for i, (kind, turn, parent) in enumerate(RECORDING, start=1)
    ]


def event(seq, kind, payload, turn=1):
    return AgentEventV1(
        session_id=str(SESSION),
        seq=seq,
        type=kind,
        ts="2026-11-25T11:24:03.120Z",
        turn=turn,
        payload=validate_payload(kind, payload),
    )


def test_the_recording_folds_to_a_complete_state():
    state = reduce_events(SESSION, recorded())
    assert state.as_of_seq == len(RECORDING) and state.turn == 1
    assert state.status == "running" and state.mode == "plan"
    assert [m.role for m in state.messages] == ["user", "assistant"]
    assert state.messages[1].text == "Looking at it." and state.messages[1].completed
    assert state.tools[0].status == "ok" and state.tools[0].summary == "read 40 lines"
    assert state.approvals[0].status == "approved"
    assert state.checkpoints == ("cp1",) and state.last_error_code == "model_unavailable"


@pytest.mark.parametrize("cut", range(0, len(RECORDING) + 1))
def test_a_client_that_disconnects_at_any_seq_reaches_the_same_state(cut):
    events = recorded()
    whole = reduce_events(SESSION, events)
    first = reduce_events(SESSION, events[:cut])
    # At-least-once delivery: the reconnect re-sends up to two events the client already folded.
    resumed = reduce_events(SESSION, events[:cut] + events[max(0, cut - 2) :])
    assert resumed == whole and first.as_of_seq == cut


def test_duplicates_and_replays_do_not_change_the_state():
    events = recorded()
    assert reduce_events(SESSION, events + events[3:7] + events) == reduce_events(SESSION, events)


def test_deltas_are_idempotent_by_offset_and_completion_is_authoritative():
    base = [event(1, "message.started", {"message_id": "m", "model_id": "x"})]

    def d(seq, off, text):
        return event(seq, "message.delta", {"message_id": "m", "offset": off, "text": text})

    state = reduce_events(SESSION, [*base, d(2, 0, "Hel"), d(3, 3, "lo "), d(4, 3, "lo ")])
    assert state.messages[0].text == "Hello " and not state.messages[0].completed
    gap = reduce_events(SESSION, [*base, d(2, 0, "ab"), d(3, 9, "zz")])
    assert gap.messages[0].text == "ab"  # a gap is ignored until message.completed
    done = reduce_events(
        SESSION,
        [
            *base,
            d(2, 0, "ab"),
            event(
                3,
                "message.completed",
                {"message_id": "m", "text": "abcdef", "stop_reason": "end_turn"},
            ),
            d(4, 6, "late"),
        ],
    )
    assert done.messages[0].text == "abcdef" and done.messages[0].completed


def test_offsets_count_utf16_code_units():
    base = [event(1, "message.started", {"message_id": "m", "model_id": "x"})]
    emoji = event(2, "message.delta", {"message_id": "m", "offset": 0, "text": "a\U0001f600"})
    tail = event(3, "message.delta", {"message_id": "m", "offset": 3, "text": "b"})
    assert reduce_events(SESSION, [*base, emoji, tail]).messages[0].text == "a\U0001f600b"
