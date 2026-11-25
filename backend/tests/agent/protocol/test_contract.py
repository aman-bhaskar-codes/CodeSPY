# ruff: noqa: E501
"""AEP/1 contracts: every event type validates, the schema file is current, the cipher binds identity."""

import json
from pathlib import Path
from uuid import uuid4

import jsonschema
import pytest
from app.contracts.agent_events import (
    EVENT_TYPES,
    MAX_PAYLOAD_BYTES,
    PAYLOADS,
    TRANSIENT_TYPES,
    AgentEventV1,
    export_schema,
    sse_frame,
    validate_payload,
)
from app.services.agent.event_log import EventCipher
from cryptography.exceptions import InvalidTag
from pydantic import ValidationError

from tests.agent.protocol.samples import SAMPLES, TRANSIENT_SAMPLE

ROOT = Path(__file__).resolve().parents[4]
SESSION = "10000000-0000-4000-8000-000000000001"
TS = "2026-11-25T11:24:03.120Z"


def test_every_event_type_has_a_sample_and_round_trips():
    assert set(SAMPLES) | TRANSIENT_TYPES == set(EVENT_TYPES)
    for kind, payload in SAMPLES.items():
        canonical = validate_payload(kind, payload)
        assert validate_payload(kind, canonical) == canonical  # canonical form is stable
        event = AgentEventV1(session_id=SESSION, seq=1, type=kind, ts=TS, turn=1, payload=canonical)
        assert AgentEventV1.model_validate_json(event.model_dump_json()) == event


def test_samples_validate_against_the_exported_json_schema():
    schema = export_schema()
    for kind, payload in SAMPLES.items():
        resolver_schema = {**schema["payloads"][kind], "$defs": schema["$defs"]}
        jsonschema.validate(validate_payload(kind, payload), resolver_schema)
    envelope = {**schema["envelope"], "$defs": schema["$defs"]}
    event = AgentEventV1(
        session_id=SESSION, seq=1, type="session.status", ts=TS, turn=0, payload={"status": "idle"}
    )
    jsonschema.validate(json.loads(event.model_dump_json()), envelope)


def test_the_committed_schema_file_is_current():
    committed = (ROOT / "docs/contracts/AgentEventV1.schema.json").read_text()
    assert committed == json.dumps(export_schema(), sort_keys=True, indent=2) + "\n", (
        "run: uv run python scripts/export_agent_schema.py"
    )


def test_payloads_reject_unknown_fields_wrong_types_and_oversize():
    with pytest.raises(ValidationError):
        validate_payload("session.status", {"status": "running", "extra": 1})
    with pytest.raises(ValidationError):
        validate_payload("session.status", {"status": "sleeping"})
    with pytest.raises(ValidationError):
        validate_payload("message.delta", {"message_id": "m", "offset": -1, "text": "x"})
    with pytest.raises(ValueError, match="unknown event type"):
        validate_payload("made.up", {})
    big = {"tool_call_id": "t", "status": "ok", "evidence": []}
    big["summary"] = "x" * 1024
    big["evidence"] = [{"evidence_id": f"e{i}"} for i in range(50)]
    assert len(json.dumps(validate_payload("tool.call.completed", big))) < MAX_PAYLOAD_BYTES
    huge = {"plan_id": "p", "steps": [{"id": f"s{i}", "title": "t" * 500} for i in range(50)]}
    with pytest.raises(ValueError, match="16 KiB"):
        validate_payload("plan.updated", huge)


def test_provenance_is_a_tagged_union_that_admits_byte_ranges():
    ok = {
        "claim_id": "c",
        "kind": "unknown",
        "text": "t",
        "provenance": [
            {"kind": "byte_range", "artifact_id": "a", "start_offset": "0", "end_offset": "1f"}
        ],
    }
    assert validate_payload("evidence.claim", ok)["provenance"][0]["kind"] == "byte_range"
    bad = {
        "claim_id": "c",
        "kind": "unknown",
        "text": "t",
        "provenance": [
            {"kind": "byte_range", "artifact_id": "a", "start_offset": "0xZZ", "end_offset": "1"}
        ],
    }
    with pytest.raises(ValidationError):
        validate_payload("evidence.claim", bad)


def test_envelope_rules_for_seq_and_type():
    with pytest.raises(ValidationError):
        AgentEventV1(
            session_id=SESSION,
            seq=0,
            type="session.status",
            ts=TS,
            turn=0,
            payload={"status": "idle"},
        )  # stored events start at 1
    with pytest.raises(ValidationError):
        AgentEventV1(
            session_id=SESSION, seq=3, type="stream.reset", ts=TS, turn=0, payload=TRANSIENT_SAMPLE
        )  # transient events carry seq 0
    with pytest.raises(ValidationError):
        AgentEventV1(session_id=SESSION, seq=1, type="Bad Type", ts=TS, turn=0, payload={})
    future = AgentEventV1(
        session_id=SESSION, seq=9, type="future.thing", ts=TS, turn=0, payload={"anything": 1}
    )
    assert future.type == "future.thing"  # unknown types are carried, not rejected


def test_sse_frames_carry_ids_except_transient_events():
    stored = AgentEventV1(
        session_id=SESSION, seq=42, type="session.status", ts=TS, turn=0, payload={"status": "idle"}
    )
    frame = sse_frame(stored)
    assert frame.startswith("id: 42\nevent: session.status\ndata: {") and frame.endswith("\n\n")
    reset = AgentEventV1(
        session_id=SESSION, seq=0, type="stream.reset", ts=TS, turn=0, payload=TRANSIENT_SAMPLE
    )
    assert sse_frame(reset).startswith("event: stream.reset\n")  # no id, Last-Event-ID is kept


def test_the_cipher_binds_ciphertext_to_its_identity():
    cipher = EventCipher(bytes(range(32)))
    org, session = uuid4(), uuid4()
    blob = cipher.encrypt(b'{"a":1}', org, session, 5, "session.status")
    assert cipher.decrypt(blob, org, session, 5, "session.status") == b'{"a":1}'
    assert len(blob) == len(b'{"a":1}') + 28
    for args in (
        (uuid4(), session, 5, "session.status"),
        (org, uuid4(), 5, "session.status"),
        (org, session, 6, "session.status"),
        (org, session, 5, "error"),
    ):
        with pytest.raises(InvalidTag):
            cipher.decrypt(blob, *args)
    with pytest.raises(ValueError):
        EventCipher(b"short")


def test_every_registered_payload_has_a_schema_entry():
    assert set(export_schema()["payloads"]) == set(PAYLOADS)
