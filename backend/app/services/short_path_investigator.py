"""Default single-investigator short-path execution engine with deterministic tools
and checkpoints.
"""

import json
import logging
from typing import Any
from uuid import UUID, uuid4

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
from app.services.investigation_checkpointer import (
    FencedCheckpointer,
)
from app.services.investigator_tools import (
    InvestigatorToolRegistry,
    ToolExecutionContext,
)
from app.services.model_gateway import ModelGatewayService

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_TEMPLATE = """You are an elite, evidence-grounded Security Research Investigator.
Your mandate is to verify or refute security hypotheses, scan findings, and vulnerabilities.

Rules of Engagement:
1. Grounding Invariant: Never hallucinate file paths, code snippets, or line numbers.
   You MUST verify them using `search_code` and `read_file_range`.
2. Memory Integration: Use `check_memory` for prior human triage decisions or patterns.
3. Minimal Step Execution: Be direct and produce your verdict once evidence is definitive.
4. Final Structured Output: When concluded, output a valid JSON object matching this schema:
{
  "verdict": "true_positive" | "false_positive" | "indeterminate" | "benign",
  "confidence": 0.0 to 1.0,
  "summary": "Concise technical explanation citing concrete evidence and provenance",
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


class ShortPathInvestigator:
    """Executes single-investigator short-path workflows bounded by steps, budget,
    and worker lease.
    """

    def __init__(
        self,
        model_gateway: ModelGatewayService,
        checkpointer: FencedCheckpointer,
        tool_registry: InvestigatorToolRegistry | None = None,
    ) -> None:
        self.gateway = model_gateway
        self.checkpointer = checkpointer
        self.tools = tool_registry or InvestigatorToolRegistry()

    async def run_investigation(
        self,
        org_id: UUID,
        investigation_id: UUID,
        worker_id: str,
        fencing_token: int,
        disclosure_policy: DisclosurePolicyV1,
        context: ToolExecutionContext | None = None,
        system_prompt_override: str | None = None,
    ) -> InvestigationStateV1:
        inv_row = await self.checkpointer.get_investigation(org_id, investigation_id)

        # 1. Recover latest checkpoint if resuming, else initialize new state
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
                        content=system_prompt_override or SYSTEM_PROMPT_TEMPLATE,
                    ),
                    ChatMessageV1(
                        role=ModelRole.USER,
                        content=f"Investigate: {inv_row['query']}",
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

        while current_step < max_steps:
            # Check budget limit
            if state.total_spent_micros >= budget_limit:
                logger.warning(
                    "Investigation %s hit budget limit: %d >= %d micros",
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
                    summary=f"Investigation halted: budget limit ({budget_limit} micros) reached.",
                )
                return state.model_copy(
                    update={
                        "verdict": InvestigationVerdict.INDETERMINATE.value,
                        "summary": "Budget limit exhausted before conclusive proof.",
                    }
                )

            current_step += 1
            await self.checkpointer.append_event(
                org_id=org_id,
                investigation_id=investigation_id,
                step_index=current_step,
                event_type=InvestigationEventType.STEP_STARTED.value,
                node_name="investigator_loop",
                payload={"step": current_step, "spent_micros": state.total_spent_micros},
            )

            # Renew lease periodically
            await self.checkpointer.renew_lease(
                org_id=org_id,
                investigation_id=investigation_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                lease_ttl_seconds=30,
            )

            # Call model gateway
            step_idemp_key = f"inv-{investigation_id}-step-{current_step}"
            req_tools = self.tools.get_tool_definitions()

            model_response = await self.gateway.execute_call(
                org_id=org_id,
                repository_id=state.repository_id,
                principal_id=exec_context.principal_id,
                investigation_id=investigation_id,
                logical_call_id=uuid4(),
                attempt_id=1,
                messages=state.history,
                tools=req_tools,
                requirement=ModelRoutingRequirementV1(
                    estimated_context_tokens=4000,
                    estimated_output_tokens=1500,
                    requires_tools=True,
                ),
                disclosure_policy=disclosure_policy,
                idempotency_key=step_idemp_key,
            )

            new_spent = state.total_spent_micros + model_response.charge_micros
            state = state.model_copy(update={"total_spent_micros": new_spent})

            # Check if model emitted tool calls
            if model_response.tool_calls:
                new_history = list(state.history)
                new_history.append(
                    ChatMessageV1(
                        role=ModelRole.ASSISTANT,
                        content=model_response.content,
                        tool_calls=model_response.tool_calls,
                    )
                )

                new_tool_history = list(state.tool_history)
                new_evidence = list(state.collected_evidence)

                for call in model_response.tool_calls:
                    await self.checkpointer.append_event(
                        org_id=org_id,
                        investigation_id=investigation_id,
                        step_index=current_step,
                        event_type=InvestigationEventType.TOOL_CALLED.value,
                        node_name="investigator_tools",
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
                        node_name="investigator_tools",
                        payload={"tool": call.name, "result": tool_res},
                    )

                    # Add tool response to message history
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

                # Save intermediate step checkpoint
                await self.checkpointer.save_checkpoint(
                    org_id=org_id,
                    investigation_id=investigation_id,
                    worker_id=worker_id,
                    fencing_token=fencing_token,
                    step_index=current_step,
                    graph_node="tool_execution",
                    state=state,
                    new_spent_micros=model_response.charge_micros,
                )
                continue

            # Model produced a terminal or textual response
            parsed_verdict = self._extract_verdict_payload(model_response.content)
            if parsed_verdict:
                v_str = parsed_verdict.get("verdict", InvestigationVerdict.INDETERMINATE.value)
                v_conf = float(parsed_verdict.get("confidence", 0.8))
                v_summ = parsed_verdict.get("summary", model_response.content)

                raw_claims = parsed_verdict.get("claims", [])
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

                state = state.model_copy(
                    update={
                        "current_step": current_step,
                        "verdict": v_str,
                        "verdict_confidence": v_conf,
                        "summary": v_summ,
                        "claims": tuple(extracted_claims),
                    }
                )

                # Save final checkpoint
                await self.checkpointer.save_checkpoint(
                    org_id=org_id,
                    investigation_id=investigation_id,
                    worker_id=worker_id,
                    fencing_token=fencing_token,
                    step_index=current_step,
                    graph_node="final_verdict",
                    state=state,
                    new_spent_micros=model_response.charge_micros,
                )

                # Mark investigation complete
                await self.checkpointer.complete_investigation(
                    org_id=org_id,
                    investigation_id=investigation_id,
                    worker_id=worker_id,
                    fencing_token=fencing_token,
                    verdict=v_str,
                    verdict_confidence=v_conf,
                    summary=v_summ,
                )
                return state

        # Step limit reached without a structured terminal verdict
        fallback_summary = "Maximum steps reached without conclusive verdict."
        await self.checkpointer.complete_investigation(
            org_id=org_id,
            investigation_id=investigation_id,
            worker_id=worker_id,
            fencing_token=fencing_token,
            verdict=InvestigationVerdict.INDETERMINATE.value,
            verdict_confidence=0.5,
            summary=fallback_summary,
        )
        return state.model_copy(
            update={
                "current_step": current_step,
                "verdict": InvestigationVerdict.INDETERMINATE.value,
                "summary": fallback_summary,
            }
        )

    def _extract_verdict_payload(self, content: str) -> dict[str, Any] | None:
        """Parses structured verdict json from model response content."""
        if not content:
            return None
        content = content.strip()
        # Look for JSON block
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and "verdict" in parsed:
                return parsed
        except (json.JSONDecodeError, TypeError, KeyError):
            logger.debug("Response content is not valid verdict JSON: %s", content[:100])
        return None
