"""Two-specialist adversarial reasoning architecture for security investigations.

Coordinates a Hypothesis Specialist (vulnerability advocate) and an Adversarial
Specialist (skeptic/defender) before synthesizing with an Arbiter under a shared
budget and worker lease.
"""

import json
import logging
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from app.contracts.investigation import (
    InvestigationClaimV1,
    InvestigationEventType,
    InvestigationStateV1,
    InvestigationVerdict,
)
from app.contracts.model import (
    ChatMessageV1,
    ModelRole,
    ModelRoutingRequirementV1,
)
from app.contracts.provider_budget import DisclosurePolicyV1
from app.ports.investigation_checkpointer import InvestigationCheckpointerPort
from app.ports.model import ModelGatewayPort
from app.services.claim_verification import ClaimVerificationService
from app.services.investigator_tools import (
    InvestigatorToolRegistry,
    ToolExecutionContext,
)

logger = logging.getLogger(__name__)

HYPOTHESIS_SPECIALIST_PROMPT = """You are a Security Hypothesis Specialist.
Your goal is to investigate vulnerabilities and construct concrete exploit/flow hypotheses.
Rules of Engagement:
1. Grounding Invariant: Never hallucinate paths/lines. Verify via `search_code`/`read_file_range`.
2. Present evidence of where attacker input enters, propagates, and reaches potential sinks.
3. Formulate your findings clearly so an adversarial skeptic can scrutinize your claims.
"""

ADVERSARIAL_SPECIALIST_PROMPT = """You are an Adversarial Security Specialist and Defender.
Your goal is to critically scrutinize the hypothesis proposed by the Hypothesis Specialist.
Rules of Engagement:
1. Actively search for mitigating controls: validation, sanitizers, auth checks, dead code paths.
2. Verify all counter-evidence using `search_code` and `read_file_range`.
3. Provide concrete counter-evidence showing whether the finding is mitigated or false positive.
"""

ARBITER_SYNTHESIS_PROMPT = """You are the Lead Security Arbiter.
You evaluate the findings of both the Hypothesis Specialist and the Adversarial Specialist.
Rules of Engagement:
1. Weigh the evidence and counter-evidence objectively.
2. If counter-evidence or mitigations disprove the exploit, conclude with false_positive or benign.
3. If the vulnerability is verified and unmitigated, conclude with "true_positive".
4. If evidence is inconclusive or missing, conclude with "indeterminate".
5. Output a valid JSON object matching this schema:
{
  "verdict": "true_positive" | "false_positive" | "indeterminate" | "benign",
  "confidence": 0.0 to 1.0,
  "summary": "Technical rationale weighing hypothesis vs adversarial challenge",
  "claims": [
    {
      "claim_id": "c1",
      "statement": "Concrete factual statement",
      "file_path": "path/to/file",
      "line_start": 10,
      "line_end": 20,
      "quoted_source": "exact code excerpt"
    }
  ]
}
"""


class TwoSpecialistInvestigator:
    """Executes a two-specialist (hypothesis vs adversary) investigation bounded
    by a shared cost budget and worker fencing token.
    """

    def __init__(
        self,
        model_gateway: ModelGatewayPort,
        checkpointer: InvestigationCheckpointerPort,
        tool_registry: InvestigatorToolRegistry | None = None,
        claim_verifier: ClaimVerificationService | None = None,
    ) -> None:
        self.gateway = model_gateway
        self.checkpointer = checkpointer
        self.tools = tool_registry or InvestigatorToolRegistry()
        self.claim_verifier = claim_verifier or ClaimVerificationService()

    async def run_investigation(
        self,
        org_id: UUID,
        investigation_id: UUID,
        worker_id: str,
        fencing_token: int,
        disclosure_policy: DisclosurePolicyV1,
        context: ToolExecutionContext | None = None,
    ) -> InvestigationStateV1:
        inv_row = await self.checkpointer.get_investigation(org_id, investigation_id)

        # 1. Recover or initialize state
        recovered = await self.checkpointer.load_latest_checkpoint(org_id, investigation_id)
        if recovered:
            current_step, state = recovered
        else:
            current_step = 0
            state = InvestigationStateV1(
                investigation_id=investigation_id,
                org_id=org_id,
                repository_id=UUID(str(inv_row["repository_id"])),
                finding_id=UUID(str(inv_row["finding_id"])) if inv_row["finding_id"] else None,
                target_type=inv_row["target_type"],
                query=inv_row["query"],
                mode=inv_row["mode"],
                depth=inv_row["depth"],
                current_step=0,
                max_steps=inv_row["max_steps"],
                history=(
                    ChatMessageV1(
                        role=ModelRole.SYSTEM,
                        content=HYPOTHESIS_SPECIALIST_PROMPT,
                    ),
                    ChatMessageV1(
                        role=ModelRole.USER,
                        content=f"Investigate hypothesis: {inv_row['query']}",
                    ),
                ),
            )

        exec_context = context or ToolExecutionContext(
            org_id=org_id,
            repository_id=state.repository_id,
            principal_id=UUID(str(inv_row["principal_id"])),
        )

        max_steps = inv_row["max_steps"]
        budget_limit = inv_row["budget_limit_micros"]

        # Stage 1: Hypothesis Specialist (up to half of max steps)
        hypothesis_limit = max(1, max_steps // 2)
        hypothesis_text = ""

        while current_step < hypothesis_limit:
            if state.total_spent_micros >= budget_limit:
                return await self._halt_for_budget(
                    org_id, investigation_id, worker_id, fencing_token, budget_limit, state
                )

            current_step += 1
            await self.checkpointer.append_event(
                org_id=org_id,
                investigation_id=investigation_id,
                step_index=current_step,
                event_type=InvestigationEventType.STEP_STARTED.value,
                node_name="hypothesis_specialist",
                payload={"step": current_step, "phase": "hypothesis_generation"},
            )

            await self.checkpointer.renew_lease(
                org_id=org_id,
                investigation_id=investigation_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                lease_ttl_seconds=150,
            )

            step_key = f"inv-{investigation_id}-hypo-{current_step}"
            resp = await self.gateway.execute_call(
                org_id=org_id,
                repository_id=state.repository_id,
                principal_id=exec_context.principal_id,
                investigation_id=investigation_id,
                logical_call_id=uuid5(NAMESPACE_URL, f"{org_id}:{step_key}"),
                attempt_id=1,
                messages=state.history,
                tools=self.tools.get_tool_definitions(),
                requirement=ModelRoutingRequirementV1(
                    task_type="planner",
                    estimated_context_tokens=4000,
                    estimated_output_tokens=1500,
                    requires_tools=True,
                ),
                disclosure_policy=disclosure_policy,
                idempotency_key=step_key,
            )

            state = state.model_copy(
                update={"total_spent_micros": state.total_spent_micros + resp.charge_micros}
            )

            if resp.tool_calls:
                state = await self._execute_tools_and_checkpoint(
                    org_id,
                    investigation_id,
                    worker_id,
                    fencing_token,
                    current_step,
                    resp,
                    exec_context,
                    state,
                    "hypothesis_specialist",
                )
                continue

            # Hypothesis specialist completed initial narrative
            hypothesis_text = resp.content
            break

        # Stage 2: Adversarial Specialist
        adversary_limit = max(current_step + 1, max_steps - 1)
        adversary_history = [
            ChatMessageV1(role=ModelRole.SYSTEM, content=ADVERSARIAL_SPECIALIST_PROMPT),
            ChatMessageV1(
                role=ModelRole.USER,
                content=(
                    f"Scrutinize and attempt to refute this hypothesis:\n"
                    f"Target Query: {state.query}\n"
                    f"Hypothesis Narrative:\n{hypothesis_text or 'No direct hypothesis output.'}"
                ),
            ),
        ]

        adversary_text = ""
        while current_step < adversary_limit:
            if state.total_spent_micros >= budget_limit:
                return await self._halt_for_budget(
                    org_id, investigation_id, worker_id, fencing_token, budget_limit, state
                )

            current_step += 1
            await self.checkpointer.append_event(
                org_id=org_id,
                investigation_id=investigation_id,
                step_index=current_step,
                event_type=InvestigationEventType.STEP_STARTED.value,
                node_name="adversary_specialist",
                payload={"step": current_step, "phase": "adversarial_challenge"},
            )

            await self.checkpointer.renew_lease(
                org_id=org_id,
                investigation_id=investigation_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                lease_ttl_seconds=150,
            )

            step_key = f"inv-{investigation_id}-adv-{current_step}"
            resp = await self.gateway.execute_call(
                org_id=org_id,
                repository_id=state.repository_id,
                principal_id=exec_context.principal_id,
                investigation_id=investigation_id,
                logical_call_id=uuid5(NAMESPACE_URL, f"{org_id}:{step_key}"),
                attempt_id=1,
                messages=tuple(adversary_history),
                tools=self.tools.get_tool_definitions(),
                requirement=ModelRoutingRequirementV1(
                    task_type="auditor",
                    estimated_context_tokens=4000,
                    estimated_output_tokens=1500,
                    requires_tools=True,
                ),
                disclosure_policy=disclosure_policy,
                idempotency_key=step_key,
            )

            state = state.model_copy(
                update={"total_spent_micros": state.total_spent_micros + resp.charge_micros}
            )

            if resp.tool_calls:
                adversary_history.append(
                    ChatMessageV1(
                        role=ModelRole.ASSISTANT,
                        content=resp.content,
                        tool_calls=resp.tool_calls,
                        continuation_ref=resp.continuation_ref,
                    )
                )
                for call in resp.tool_calls:
                    await self.checkpointer.append_event(
                        org_id=org_id,
                        investigation_id=investigation_id,
                        step_index=current_step,
                        event_type=InvestigationEventType.TOOL_CALLED.value,
                        node_name="adversary_specialist",
                        payload={"tool": call.name, "arguments": call.arguments_json},
                    )

                    args_dict: dict[str, Any]
                    try:
                        args_dict = json.loads(call.arguments_json)
                    except Exception:
                        args_dict = {}

                    t_res = await self.tools.execute_tool(call.name, args_dict, exec_context)

                    await self.checkpointer.append_event(
                        org_id=org_id,
                        investigation_id=investigation_id,
                        step_index=current_step,
                        event_type=InvestigationEventType.TOOL_RESULT.value,
                        node_name="adversary_specialist",
                        payload={"tool": call.name, "result": t_res},
                    )

                    adversary_history.append(
                        ChatMessageV1(
                            role=ModelRole.TOOL,
                            content=json.dumps(t_res),
                            name=call.name,
                            tool_call_id=call.id,
                        )
                    )
                continue

            adversary_text = resp.content
            break

        # Stage 3: Arbiter Synthesis
        current_step += 1
        await self.checkpointer.append_event(
            org_id=org_id,
            investigation_id=investigation_id,
            step_index=current_step,
            event_type=InvestigationEventType.STEP_STARTED.value,
            node_name="arbiter",
            payload={"step": current_step, "phase": "arbiter_synthesis"},
        )

        arbiter_messages = [
            ChatMessageV1(role=ModelRole.SYSTEM, content=ARBITER_SYNTHESIS_PROMPT),
            ChatMessageV1(
                role=ModelRole.USER,
                content=(
                    f"Target Query: {state.query}\n\n"
                    f"--- HYPOTHESIS SPECIALIST ---\n"
                    f"{hypothesis_text or 'No hypothesis narrative.'}\n\n"
                    f"--- ADVERSARIAL SPECIALIST ---\n"
                    f"{adversary_text or 'No adversarial counter-evidence.'}\n\n"
                    f"Synthesize the debate and deliver your structured verdict."
                ),
            ),
        ]

        step_key = f"inv-{investigation_id}-arbiter-{current_step}"
        arbiter_resp = await self.gateway.execute_call(
            org_id=org_id,
            repository_id=state.repository_id,
            principal_id=exec_context.principal_id,
            investigation_id=investigation_id,
            logical_call_id=uuid5(NAMESPACE_URL, f"{org_id}:{step_key}"),
            attempt_id=1,
            messages=tuple(arbiter_messages),
            tools=(),
            requirement=ModelRoutingRequirementV1(
                task_type="synthesizer",
                estimated_context_tokens=5000,
                estimated_output_tokens=2000,
                requires_tools=False,
            ),
            disclosure_policy=disclosure_policy,
            idempotency_key=step_key,
        )

        state = state.model_copy(
            update={"total_spent_micros": state.total_spent_micros + arbiter_resp.charge_micros}
        )

        # Parse arbiter verdict & extract claims
        parsed = self._extract_verdict_payload(arbiter_resp.content) or {}
        verdict = parsed.get("verdict", InvestigationVerdict.INDETERMINATE.value)
        confidence = float(parsed.get("confidence", 0.7))
        summary = parsed.get("summary", arbiter_resp.content)

        raw_claims = parsed.get("claims", [])
        extracted_claims = [
            InvestigationClaimV1(
                claim_id=c.get("claim_id", f"c-{i + 1}"),
                statement=c.get("statement", ""),
                file_path=c.get("file_path"),
                line_start=c.get("line_start"),
                line_end=c.get("line_end"),
                quoted_source=c.get("quoted_source"),
                status="pending",
            )
            for i, c in enumerate(raw_claims)
        ]

        if extracted_claims:
            await self.checkpointer.append_event(
                org_id=org_id,
                investigation_id=investigation_id,
                step_index=current_step,
                event_type=InvestigationEventType.CLAIM_EXTRACTED.value,
                node_name="claim_extractor",
                payload={"total_claims": len(extracted_claims)},
            )

        # Claim verification against ground truth
        verified_claims = extracted_claims
        if self.claim_verifier and extracted_claims:
            await self.checkpointer.append_event(
                org_id=org_id,
                investigation_id=investigation_id,
                step_index=current_step,
                event_type=InvestigationEventType.VERIFICATION_STARTED.value,
                node_name="claim_verifier",
                payload={"claims_to_verify": len(extracted_claims)},
            )

            report = await self.claim_verifier.verify_claims(
                claims=extracted_claims,
                context=exec_context,
                original_verdict=verdict,
                original_confidence=confidence,
            )

            await self.checkpointer.append_event(
                org_id=org_id,
                investigation_id=investigation_id,
                step_index=current_step,
                event_type=InvestigationEventType.VERIFICATION_RESULT.value,
                node_name="claim_verifier",
                payload={
                    "is_grounded": report.is_grounded,
                    "grounding_ratio": report.grounding_ratio,
                    "verified_count": report.verified_count,
                    "refuted_count": report.refuted_count,
                    "unsupported_count": report.unsupported_count,
                    "adjusted_verdict": report.adjusted_verdict,
                    "summary": report.verification_summary,
                },
            )

            verified_claims = list(report.verified_claims)
            if report.adjusted_verdict is not None:
                verdict = report.adjusted_verdict
            if report.adjusted_confidence is not None:
                confidence = report.adjusted_confidence
            summary = f"{summary}\n\nGrounding: {report.verification_summary}"

        # Complete investigation
        await self.checkpointer.complete_investigation(
            org_id=org_id,
            investigation_id=investigation_id,
            worker_id=worker_id,
            fencing_token=fencing_token,
            verdict=verdict,
            verdict_confidence=confidence,
            summary=summary,
        )

        await self.checkpointer.append_event(
            org_id=org_id,
            investigation_id=investigation_id,
            step_index=current_step,
            event_type=InvestigationEventType.VERDICT_REACHED.value,
            node_name="arbiter",
            payload={
                "verdict": verdict,
                "confidence": confidence,
                "summary": summary,
                "mode": "two_specialist",
            },
        )

        return state.model_copy(
            update={
                "current_step": current_step,
                "verdict": verdict,
                "verdict_confidence": confidence,
                "summary": summary,
                "claims": tuple(verified_claims),
            }
        )

    async def _execute_tools_and_checkpoint(
        self,
        org_id: UUID,
        investigation_id: UUID,
        worker_id: str,
        fencing_token: int,
        current_step: int,
        resp: Any,
        exec_context: ToolExecutionContext,
        state: InvestigationStateV1,
        node_name: str,
    ) -> InvestigationStateV1:
        new_history = list(state.history)
        new_history.append(
            ChatMessageV1(
                role=ModelRole.ASSISTANT,
                content=resp.content,
                tool_calls=resp.tool_calls,
                continuation_ref=resp.continuation_ref,
            )
        )
        new_tool_history = list(state.tool_history)
        new_evidence = list(state.collected_evidence)

        for call in resp.tool_calls:
            await self.checkpointer.append_event(
                org_id=org_id,
                investigation_id=investigation_id,
                step_index=current_step,
                event_type=InvestigationEventType.TOOL_CALLED.value,
                node_name=node_name,
                payload={"tool": call.name, "arguments": call.arguments_json},
            )

            args_dict: dict[str, Any]
            try:
                args_dict = json.loads(call.arguments_json)
            except Exception:
                args_dict = {}

            tool_res = await self.tools.execute_tool(call.name, args_dict, exec_context)

            await self.checkpointer.append_event(
                org_id=org_id,
                investigation_id=investigation_id,
                step_index=current_step,
                event_type=InvestigationEventType.TOOL_RESULT.value,
                node_name=node_name,
                payload={"tool": call.name, "result": tool_res},
            )

            new_history.append(
                ChatMessageV1(
                    role=ModelRole.TOOL,
                    content=json.dumps(tool_res),
                    name=call.name,
                    tool_call_id=call.id,
                )
            )
            new_tool_history.append(
                {
                    "step": current_step,
                    "tool": call.name,
                    "arguments": args_dict,
                    "result": tool_res,
                }
            )
            if tool_res.get("status") == "ok":
                new_evidence.append(
                    {"step": current_step, "source_tool": call.name, "data": tool_res}
                )

        state = state.model_copy(
            update={
                "current_step": current_step,
                "history": tuple(new_history),
                "tool_history": tuple(new_tool_history),
                "collected_evidence": tuple(new_evidence),
            }
        )

        await self.checkpointer.save_checkpoint(
            org_id=org_id,
            investigation_id=investigation_id,
            worker_id=worker_id,
            fencing_token=fencing_token,
            step_index=current_step,
            graph_node=node_name,
            state=state,
            new_spent_micros=resp.charge_micros,
        )
        return state

    async def _halt_for_budget(
        self,
        org_id: UUID,
        investigation_id: UUID,
        worker_id: str,
        fencing_token: int,
        budget_limit: int,
        state: InvestigationStateV1,
    ) -> InvestigationStateV1:
        logger.warning(
            "TwoSpecialist investigation %s hit budget limit: %d >= %d micros",
            investigation_id,
            state.total_spent_micros,
            budget_limit,
        )
        await self.checkpointer.complete_investigation(
            org_id=org_id,
            investigation_id=investigation_id,
            worker_id=worker_id,
            fencing_token=fencing_token,
            verdict=InvestigationVerdict.INDETERMINATE.value,
            verdict_confidence=0.0,
            summary=f"Investigation halted: shared budget limit ({budget_limit} micros) reached.",
        )
        return state.model_copy(
            update={
                "verdict": InvestigationVerdict.INDETERMINATE.value,
                "summary": "Shared budget limit exhausted before conclusive proof.",
            }
        )

    def _extract_verdict_payload(self, text: str) -> dict[str, Any] | None:
        if not text:
            return None
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                candidate = json.loads(text[start : end + 1])
                if isinstance(candidate, dict) and "verdict" in candidate:
                    return candidate
            except Exception:
                logger.debug("model_verdict_json_invalid")
        return None
