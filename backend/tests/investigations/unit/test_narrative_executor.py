# ruff: noqa: E501
"""The report narrative mode of the executor, run in memory with a scripted model and a fake report tool."""

import hashlib
import json
from uuid import UUID, uuid4

from app.analyzers.graph.render import statement_text
from app.analyzers.graph.statement_ids import statement_ids
from app.contracts.investigation_execution import (
    InvestigationAdvanceStatus,
    InvestigationEvidenceV1,
    InvestigationLimitsV1,
    InvestigationSpecV1,
    InvestigationStopReason,
    InvestigationToolResultV1,
    InvestigationToolStatus,
)
from app.ports.investigation_execution import InvestigationToolPort
from app.services.investigation_executor import (
    InMemoryInvestigationAuthority,
    InMemoryInvestigationCheckpointRepo,
    InMemoryInvestigationStepReceiptRepo,
    InvestigationExecutorService,
)
from app.services.investigation_journal import InMemoryExecutionJournal
from app.services.investigation_tools import REPORT_SECTIONS

from tests.investigations.unit.test_executor import DummyModelGateway, _make_context, _make_response
from tests.phase3.unit.test_architecture_report import build

DIGEST = "d" * 64


def narrative_spec(max_steps=25, budget=5_000_000) -> InvestigationSpecV1:
    return InvestigationSpecV1(
        org_id=uuid4(),
        repository_id=uuid4(),
        snapshot_id=uuid4(),
        principal_id=uuid4(),
        target_type="architecture_report",
        mode="report_narrative",
        query="Narrate the report.",
        limits=InvestigationLimitsV1(
            max_steps=max_steps,
            budget_limit_micros=budget,
            max_context_tokens=8192,
            max_context_bytes=65536,
        ),
        disclosure_policy_digest="a" * 64,
        tool_policy_revision="v1",
        model_route_revision="v1",
        model_profile_revision="v1",
        runtime_revision=1,
        privacy_policy_revision="v1",
        architecture_report_id=uuid4(),
        architecture_report_digest=DIGEST,
    )


class ReportTools(InvestigationToolPort):
    """Serves pages the way the real tool does and keeps the receipts it would write."""

    def __init__(self, report, scope, status=InvestigationToolStatus.OK):
        self.sids = statement_ids(report)
        self.scope, self.status = scope, status
        self.rows: list[InvestigationEvidenceV1] = []
        self.requests = []

    async def execute(self, request, context):
        self.requests.append(request.arguments)
        if self.status != InvestigationToolStatus.OK:
            return InvestigationToolResultV1(
                status=self.status, omission_reason="report_unavailable"
            )
        section, offset = request.arguments["section"], request.arguments["offset"]
        rows = self.sids[section][offset : offset + 3]  # small pages exercise the paging
        nxt = offset + 3 if offset + 3 < len(self.sids[section]) else None
        entries = [
            {
                "sid": sid,
                "kind": s.kind,
                "code": s.code,
                "text": statement_text(s),
                "params": s.params,
            }
            for sid, s in rows
        ]
        payload = json.dumps(
            {
                "section": section,
                "total": len(self.sids[section]),
                "offset": offset,
                "next_offset": nxt,
                "statements": entries,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        sha = hashlib.sha256(payload.encode()).hexdigest()
        ids = []
        for e in entries:
            row = InvestigationEvidenceV1(
                id=uuid4(),
                org_id=self.scope.org_id,
                investigation_id=self.scope.investigation_id,
                snapshot_id=self.scope.snapshot_id,
                observation_type="report_fact",
                fact_ref=f"{DIGEST}:{e['sid']}",
                excerpt_sha256=sha,
            )
            self.rows.append(row)
            ids.append(row.id)
        return InvestigationToolResultV1(
            status=InvestigationToolStatus.PARTIAL
            if nxt is not None
            else InvestigationToolStatus.OK,
            payload=payload,
            evidence_refs=tuple(ids),
        )


class Evidence:
    def __init__(self, tools):
        self.tools = tools

    async def list_evidence(self, session, org_id, investigation_id):
        return list(self.tools.rows)


class EchoGateway(DummyModelGateway):
    """Writes one confirmed sentence per statement of the page it is shown."""

    def __init__(self, charge=1000, reply=None):
        super().__init__()
        self.charge, self.reply = charge, reply

    async def execute_call(self, **kw):
        self.call_history.append(
            {"messages": kw["messages"], "tools": kw["tools"], "key": kw["idempotency_key"]}
        )
        if self.reply is not None:
            return _make_response(self.reply, charge_micros=self.charge)
        user = kw["messages"][1].content
        marker = user.split(" BEGIN")[0].split("\n")[-1]
        page = json.loads(user.split(marker + " BEGIN\n")[1].split("\n" + marker + " END")[0])
        sentences = [
            {"claim_id": f"c{i}", "text": e["text"], "sids": [e["sid"]]}
            for i, e in enumerate(page["statements"])
        ]
        return _make_response(
            json.dumps({"schema_version": 1, "overview": "", "sentences": sentences}),
            charge_micros=self.charge,
        )


def make(spec, gateway, tools_status=InvestigationToolStatus.OK, journal=None, checkpoints=None):
    report, _ = build()
    ctx = _make_context(spec)
    tools = ReportTools(report, ctx.scope, tools_status)
    executor = InvestigationExecutorService(
        gateway=gateway,
        tools=tools,
        authority=InMemoryInvestigationAuthority(ctx.scope),
        checkpoint_repo=checkpoints or InMemoryInvestigationCheckpointRepo(),
        step_receipt_repo=InMemoryInvestigationStepReceiptRepo(),
        spec_provider=lambda s, o, h: spec,
        evidence_repo=Evidence(tools),
        journal=journal,
    )
    return executor, ctx, tools, report


async def run(executor, ctx, limit=60):
    for _ in range(limit):
        advance = await executor.advance(ctx)
        if advance.status != InvestigationAdvanceStatus.PROGRESS:
            return advance
    raise AssertionError("did not finish")


async def test_a_narrative_run_reads_every_page_and_verifies_every_sentence():
    gateway = EchoGateway()
    executor, ctx, tools, report = make(narrative_spec(), gateway)
    advance = await run(executor, ctx)
    assert (
        advance.status == InvestigationAdvanceStatus.READY_TO_PUBLISH and advance.draft is not None
    )
    draft = advance.draft
    total = sum(len(rows) for rows in statement_ids(report).values())
    assert (
        draft.narrative is not None and draft.narrative.total == total == draft.narrative.confirmed
    )
    assert (
        draft.narrative.grounded
        and draft.narrative.unnarrated_sections == ()
        and draft.verdict == "indeterminate"
    )
    assert (
        draft.stop_reason == InvestigationStopReason.COMPLETED
        and "[unverified]" not in draft.summary
    )
    assert next(r["section"] for r in tools.requests) == REPORT_SECTIONS[0]
    assert all(call["tools"] == () for call in gateway.call_history)  # the model never gets a tool


async def test_the_prompt_keeps_instructions_static_and_wraps_the_page_in_a_run_marker():
    gateway = EchoGateway()
    executor, ctx, _, _ = make(narrative_spec(), gateway)
    await run(executor, ctx)
    first = gateway.call_history[0]["messages"]
    assert (
        first[0].role.value == "system"
        and "DATA" in first[0].content
        and "$" not in first[0].content
    )
    user = first[1].content
    marker = next(
        line for line in user.splitlines() if line.startswith("=====") and line.endswith(" BEGIN")
    )[:-6]
    assert user.count(marker) == 2 and marker not in first[0].content
    keys = {c["key"] for c in gateway.call_history}
    assert len(keys) == len(gateway.call_history)  # one idempotency key per page


async def test_fabricated_citations_and_unparseable_replies_never_become_confirmed():
    fabricated = json.dumps(
        {
            "schema_version": 1,
            "sentences": [{"claim_id": "x", "text": "Everything is perfect.", "sids": ["f" * 16]}],
        }
    )
    executor, ctx, _, _ = make(narrative_spec(), EchoGateway(reply=fabricated))
    draft = (await run(executor, ctx)).draft
    assert (
        draft.narrative.confirmed == 0
        and not draft.narrative.grounded
        and draft.summary.count("[unverified]") == draft.narrative.total > 0
    )

    executor, ctx, _, _ = make(
        narrative_spec(), EchoGateway(reply="I cannot comply, here is prose.")
    )
    draft = (await run(executor, ctx)).draft
    assert (
        draft.narrative.total == 0
        and draft.narrative.unparsed_units > 0
        and not draft.narrative.grounded
    )
    assert (
        draft.stop_reason == InvestigationStopReason.PARTIAL_COVERAGE
        and "could not be parsed" in draft.coverage_notes
    )


async def test_budget_exhaustion_ends_with_a_partial_but_verified_narrative():
    gateway = EchoGateway(charge=600_000)
    executor, ctx, _, _ = make(narrative_spec(budget=1_000_000), gateway)
    draft = (await run(executor, ctx)).draft
    assert draft.stop_reason == InvestigationStopReason.SPENDING_BUDGET_EXHAUSTED
    assert draft.narrative.unnarrated_sections and "Not narrated" in draft.coverage_notes
    assert draft.narrative.total == draft.narrative.confirmed > 0 and len(gateway.call_history) == 2


async def test_step_limit_ends_with_a_partial_narrative():
    executor, ctx, _, _ = make(narrative_spec(max_steps=3), EchoGateway())
    draft = (await run(executor, ctx)).draft
    assert (
        draft.stop_reason == InvestigationStopReason.STEP_BUDGET_EXHAUSTED
        and draft.narrative.unnarrated_sections
    )


async def test_an_unavailable_report_fails_the_run_without_calling_the_model():
    gateway = EchoGateway()
    executor, ctx, _, _ = make(
        narrative_spec(), gateway, tools_status=InvestigationToolStatus.DENIED
    )
    advance = await run(executor, ctx)
    assert (
        advance.status == InvestigationAdvanceStatus.TERMINAL_FAILURE and gateway.call_history == []
    )
    assert (
        advance.stop_reason == InvestigationStopReason.CONFIGURATION_MISSING
        and "report_unavailable" in advance.message
    )


async def test_a_restart_continues_from_the_checkpoint_without_paying_again():
    journal, checkpoints = InMemoryExecutionJournal(), InMemoryInvestigationCheckpointRepo()
    spec = narrative_spec()
    first = EchoGateway()
    executor, ctx, tools, report = make(spec, first, journal=journal, checkpoints=checkpoints)
    for _ in range(3):  # GATHER and two pages, then the process dies
        await executor.advance(ctx)
    paid = len(first.call_history)
    second = EchoGateway()
    resumed = InvestigationExecutorService(
        gateway=second,
        tools=tools,
        authority=InMemoryInvestigationAuthority(ctx.scope),
        checkpoint_repo=checkpoints,
        step_receipt_repo=InMemoryInvestigationStepReceiptRepo(),
        spec_provider=lambda s, o, h: spec,
        evidence_repo=Evidence(tools),
        journal=journal,
    )
    draft = (await run(resumed, ctx)).draft
    total = sum(len(rows) for rows in statement_ids(report).values())
    assert draft.narrative.total == total and draft.narrative.grounded
    assert paid == 2 and len(second.call_history) >= 1
    assert len(tools.requests) == len({(r["section"], r["offset"]) for r in tools.requests})
    assert not ({c["key"] for c in first.call_history} & {c["key"] for c in second.call_history})


def test_ids_and_states_of_other_modes_are_unchanged():
    from app.contracts.investigation_execution import (
        InvestigationExecutionStateV1,
        InvestigationNodeKind,
    )

    base = InvestigationExecutionStateV1(
        scope_hash="a" * 64,
        spec_hash="b" * 64,
        execution_revision=1,
        next_node_kind=InvestigationNodeKind.GATHER,
        next_role="x",
        next_ordinal=0,
        checkpoint_sequence=0,
    )
    dump = base.model_dump()
    del dump["narrative_section"], dump["narrative_offset"]
    from app.domain.canonical_json import canonical_bytes

    assert base.compute_state_digest() == hashlib.sha256(canonical_bytes(dump)).hexdigest()
    assert UUID(int=0)
