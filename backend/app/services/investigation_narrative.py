"""Report narrative nodes for the investigation executor.

A narrative run is GATHER, then one NARRATE node per page of report statements (read the page
through the report tool, ask the model for cited sentences, tools off), then a deterministic
VERIFY. Every effect is journaled under ids derived from the node id, so a restart replays and
never pays twice.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid5

from app.agents.prompts import PromptAsset, boundary_marker, compact_schema, load_prompt, render
from app.contracts.investigation_execution import (
    InvestigationAdvanceStatus,
    InvestigationAdvanceV1,
    InvestigationEvidenceV1,
    InvestigationExecutionContextV1,
    InvestigationExecutionStateV1,
    InvestigationNodeKind,
    InvestigationReportDraftV1,
    InvestigationSpecV1,
    InvestigationStopReason,
    InvestigationToolName,
    InvestigationToolRequestV1,
    InvestigationToolStatus,
)
from app.contracts.jobs import AttemptLeaseV2
from app.contracts.model import ChatMessageV1, ModelResponseV1, ModelRole
from app.contracts.report_narrative import (
    MAX_SENTENCES,
    NarrativeConclusionV1,
    NarrativeDraftV1,
    NarrativeSentenceV1,
)
from app.domain.canonical_json import canonical_bytes, semantic_digest
from app.domain.errors import InvalidTransition
from app.services.investigation_tools import REPORT_SECTIONS
from app.services.narrative_parsing import NarrativeParseError, parse_conclusion
from app.services.narrative_verification import (
    delivered_statements,
    render_narrative,
    verify_narrative,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.investigation_executor import InvestigationExecutorService

PROMPT_NAME, PROMPT_VERSION = "report_narrative", "v1"
NARRATOR_ROLE = "narrator"
MAX_EVIDENCE_REFS = 200


def narrative_asset() -> PromptAsset:
    return load_prompt(PROMPT_NAME, PROMPT_VERSION)


def route_revision() -> str:
    """What a narrative spec records as its model route: the prompt it was created under."""
    return f"{PROMPT_NAME}:{narrative_asset().revision}"


def prompt_messages(
    asset: PromptAsset, *, section: str, page: str, node_id: UUID, report_digest: str, unit: int
) -> list[ChatMessageV1]:
    marker = boundary_marker(str(node_id), report_digest)
    user = render(
        asset,
        "section.md.tmpl",
        marker=marker,
        data=page,
        section=section,
        schema=compact_schema(asset),
        page_number=str(unit),
    )
    return [
        ChatMessageV1(role=ModelRole.SYSTEM, content=asset.text("system.md")),
        ChatMessageV1(role=ModelRole.USER, content=user),
    ]


async def narrate_node(
    ex: InvestigationExecutorService,
    session: AsyncSession | None,
    state: InvestigationExecutionStateV1,
    context: InvestigationExecutionContextV1,
    lease: AttemptLeaseV2,
    spec: InvestigationSpecV1,
    node_id: UUID,
    ordinal: int,
    role: str,
) -> InvestigationAdvanceV1:
    """Read one page of report statements and have the model write cited sentences about it."""
    scope = context.scope
    assert ex.journal is not None and spec.architecture_report_digest is not None
    index = state.narrative_section or 0
    offset = state.narrative_offset or 0
    section = REPORT_SECTIONS[index]

    request = InvestigationToolRequestV1(
        tool_name=InvestigationToolName.READ_REPORT_SECTION,
        arguments={"section": section, "offset": offset},
    )
    identity = uuid5(node_id, "tool:0:narrate")
    digest = semantic_digest(request.model_dump(mode="json"))
    effect = await ex.journal.get(scope, identity)
    if effect is not None:
        if effect["kind"] != "tool" or effect["input_digest"] != digest:
            raise InvalidTransition("execution_replay_conflict")
        result = effect["payload"]["result"]
    else:
        tool_context = context.model_copy(
            update={
                "logical_node_id": node_id,
                "logical_ordinal": ordinal,
                "node_kind": InvestigationNodeKind.SYNTHESIZE,
            }
        )
        tool_result = await ex.tools.execute(request, tool_context)
        result = tool_result.model_dump(mode="json")
        await ex.journal.put(
            scope, lease, identity, "tool", digest, {"call_id": "narrate", "result": result}
        )
    if result["status"] not in (
        InvestigationToolStatus.OK.value,
        InvestigationToolStatus.PARTIAL.value,
    ):
        return InvestigationAdvanceV1(
            status=InvestigationAdvanceStatus.TERMINAL_FAILURE,
            node_id=node_id,
            checkpoint_sequence=state.checkpoint_sequence,
            stop_reason=InvestigationStopReason.CONFIGURATION_MISSING,
            message=f"report unavailable: {result.get('omission_reason') or result['status']}",
        )
    page = json.loads(result["payload"])
    evidence_ids = [*state.committed_evidence_ids, *(UUID(e) for e in result["evidence_refs"])]
    tool_ids = [*state.committed_tool_receipt_ids, identity]
    spent = state.usage_projection_micros
    model_ids = list(state.committed_model_receipt_ids)
    input_digest = hashlib.sha256(canonical_bytes(result["payload"])).hexdigest()

    if page["statements"]:
        messages = prompt_messages(
            narrative_asset(),
            section=section,
            page=result["payload"],
            node_id=node_id,
            report_digest=spec.architecture_report_digest,
            unit=ordinal,
        )
        if (
            len(canonical_bytes([m.model_dump() for m in messages]))
            > context.limits.max_context_bytes
        ):
            return InvestigationAdvanceV1(
                status=InvestigationAdvanceStatus.TERMINAL_FAILURE,
                node_id=node_id,
                checkpoint_sequence=state.checkpoint_sequence,
                stop_reason=InvestigationStopReason.CONTEXT_BUDGET_EXCEEDED,
                message="context budget exceeded",
            )
        response, _ = await ex._call(
            session,
            state,
            context,
            lease,
            node_id,
            messages,
            (),
            "synthesizer",
            1500,
            f"inv-{scope.investigation_id}-narrate-{ordinal}",
        )
        spent += response.charge_micros
        model_ids.append(node_id)  # kept out of history: every page is an independent call

    next_offset = page["next_offset"]
    next_index, next_cursor = (index, next_offset) if next_offset is not None else (index + 1, 0)
    done = next_index >= len(REPORT_SECTIONS)
    new_state = state.model_copy(
        update={
            "next_node_kind": InvestigationNodeKind.VERIFY
            if done
            else InvestigationNodeKind.SYNTHESIZE,
            "next_role": "verifier" if done else NARRATOR_ROLE,
            "next_ordinal": ordinal + 1,
            "usage_projection_micros": spent,
            "committed_evidence_ids": tuple(evidence_ids),
            "committed_tool_receipt_ids": tuple(tool_ids),
            "committed_model_receipt_ids": tuple(model_ids),
            "narrative_section": next_index,
            "narrative_offset": next_cursor,
        }
    )
    await ex.step_receipt_repo.record_receipt(
        session=session,
        org_id=scope.org_id,
        investigation_id=scope.investigation_id,
        execution_revision=scope.execution_revision,
        node_id=node_id,
        node_kind=InvestigationNodeKind.SYNTHESIZE.value,
        role=role,
        logical_ordinal=ordinal,
        input_digest=input_digest,
        state="completed",
        attempt_id=context.attempt_id,
    )
    cp = await ex._append_checkpoint(
        session, lease, expected_previous_sequence=state.checkpoint_sequence, state=new_state
    )
    return InvestigationAdvanceV1(
        status=InvestigationAdvanceStatus.PROGRESS,
        node_id=node_id,
        checkpoint_sequence=cp.checkpoint_sequence,
    )


async def verify_narrative_node(
    ex: InvestigationExecutorService,
    session: AsyncSession | None,
    state: InvestigationExecutionStateV1,
    context: InvestigationExecutionContextV1,
    lease: AttemptLeaseV2,
    spec: InvestigationSpecV1,
    node_id: UUID,
    ordinal: int,
    role: str,
) -> InvestigationAdvanceV1:
    """Deterministic verification of everything the model wrote; no model call."""
    scope = context.scope
    assert ex.journal is not None and spec.architecture_report_digest is not None
    records: tuple[InvestigationEvidenceV1, ...] = ()
    if ex.evidence_repo is not None:
        delivered_ids = set(state.committed_evidence_ids)
        records = tuple(
            e
            for e in await ex.evidence_repo.list_evidence(
                session, scope.org_id, scope.investigation_id
            )
            if e.id in delivered_ids
        )
    pages: dict[UUID, str] = {}
    for identity in state.committed_tool_receipt_ids:
        result = (await ex._effect(scope, identity))["payload"]["result"]
        if result["status"] in ("ok", "partial"):
            for evidence_id in result["evidence_refs"]:
                pages[UUID(evidence_id)] = result["payload"]
    shown = delivered_statements(
        evidence=records,
        delivered=pages,
        org_id=scope.org_id,
        investigation_id=scope.investigation_id,
        snapshot_id=scope.snapshot_id,
        report_digest=spec.architecture_report_digest,
    )

    sentences: list[NarrativeSentenceV1] = []
    unparsed = 0
    for unit, model_id in enumerate(state.committed_model_receipt_ids):
        retained = await ex._effect(scope, model_id)
        response = ModelResponseV1.model_validate_json(json.dumps(retained["payload"]))
        try:
            conclusion = parse_conclusion(response.content)
        except NarrativeParseError:
            unparsed += 1
            continue
        # Ids are assigned here, not taken from the model: they are short, unique and always valid.
        sentences.extend(
            s.model_copy(update={"claim_id": f"u{unit}-{index}"})
            for index, s in enumerate(conclusion.sentences)
        )
    dropped = max(0, len(sentences) - MAX_SENTENCES)
    sentences = sentences[:MAX_SENTENCES]
    conclusion_all = NarrativeConclusionV1(sentences=tuple(sentences))
    outcome = verify_narrative(conclusion_all, shown)
    index = state.narrative_section or 0
    unnarrated = tuple(REPORT_SECTIONS[index:])
    stop = state.stop_reason or (
        InvestigationStopReason.PARTIAL_COVERAGE
        if unnarrated or unparsed or dropped
        else InvestigationStopReason.COMPLETED
    )
    narrative = NarrativeDraftV1(
        sentences=tuple(sentences),
        verifications=outcome.verifications,
        confirmed=outcome.confirmed,
        total=outcome.total,
        grounded=outcome.grounded,
        unnarrated_sections=unnarrated,
        unparsed_units=unparsed,
        dropped_sentences=dropped,
        prompt_revision=narrative_asset().revision,
        report_digest=spec.architecture_report_digest,
    )
    notes = []
    if unnarrated:
        notes.append("Not narrated: " + ", ".join(unnarrated) + ".")
    if unparsed:
        notes.append(f"{unparsed} model replies could not be parsed and were left out.")
    if dropped:
        notes.append(f"{dropped} sentences beyond the limit of {MAX_SENTENCES} were left out.")
    draft_fields: dict[str, Any] = {
        "investigation_id": scope.investigation_id,
        "spec_hash": scope.spec_hash,
        "verdict": "indeterminate",
        "verdict_confidence": 0.0,
        "summary": render_narrative(conclusion_all, outcome),
        "evidence_refs": tuple(e.id for e in records)[:MAX_EVIDENCE_REFS],
        "coverage_notes": " ".join(notes)[:4096],
        "unknowns": "Sentences that are not confirmed are shown as unverified.",
        "limitations": (
            "Each sentence is checked against the report statements it cites; "
            "the wording is the model's."
        ),
        "settled_usage_micros": max(0, state.usage_projection_micros),
        "stop_reason": stop,
        "narrative": narrative,
        "draft_digest": "0" * 64,
    }
    draft = InvestigationReportDraftV1.model_validate(draft_fields)
    draft = draft.model_copy(update={"draft_digest": draft.compute_draft_digest()})
    draft_ref = uuid5(node_id, "report-draft")
    await ex.journal.put(
        scope, lease, draft_ref, "draft", draft.draft_digest, draft.model_dump(mode="json")
    )
    new_state = state.model_copy(
        update={
            "next_node_kind": InvestigationNodeKind.FINALIZE,
            "next_role": "finalizer",
            "next_ordinal": ordinal + 1,
            "report_draft_digest": draft.draft_digest,
            "report_draft_ref": draft_ref,
        }
    )
    await ex.step_receipt_repo.record_receipt(
        session=session,
        org_id=scope.org_id,
        investigation_id=scope.investigation_id,
        execution_revision=scope.execution_revision,
        node_id=node_id,
        node_kind=InvestigationNodeKind.VERIFY.value,
        role=role,
        logical_ordinal=ordinal,
        input_digest=draft.draft_digest,
        state="completed",
        attempt_id=context.attempt_id,
    )
    cp = await ex._append_checkpoint(
        session, lease, expected_previous_sequence=state.checkpoint_sequence, state=new_state
    )
    return InvestigationAdvanceV1(
        status=InvestigationAdvanceStatus.READY_TO_PUBLISH,
        node_id=node_id,
        checkpoint_sequence=cp.checkpoint_sequence,
        stop_reason=stop,
        message="narrative verified, draft report ready",
        draft=draft,
    )
