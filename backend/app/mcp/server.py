"""Read-only MCP server and dispatch facade with trusted identity injection.

Integrates the official MCP SDK (mcp==2.3.0) with CodeSPY's authorized retrieval,
findings, memory, graph, feature trace, and investigation services.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import mcp.types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.context import ContextAssemblyRequestV1, ContextBudgetV1
from app.contracts.mcp import (
    AssembleContextInputV1,
    GetEvidenceInputV1,
    GetIndexStatusInputV1,
    GetInvestigationResultInputV1,
    GetInvestigationStatusInputV1,
    GetSourceViewInputV1,
    GraphNeighborsInputV1,
    GraphNeighborsResultV1,
    ListClaimsInputV1,
    ListClaimsResultV1,
    ListFindingsInputV1,
    ListFindingsResultV1,
    McpClaimItemV1,
    McpErrorV1,
    McpEvidenceResultV1,
    McpFindingItemV1,
    McpGraphEdgeV1,
    McpGraphNodeV1,
    QueryMemoryInputV1,
    SearchExactInputV1,
    SearchHybridInputV1,
    TraceFeatureInputV1,
)
from app.contracts.memory import MemoryQueryRequestV1
from app.contracts.search import (
    ExactSearchRequestV1,
    HybridSearchRequestV1,
    SearchScopeV1,
)
from app.contracts.source_view import (
    ByteRangeV1,
    PrivacyPolicyV1,
    SourceRepresentation,
    SourceViewRequestV1,
)
from app.contracts.trace import TraceBudgetV1
from app.domain.errors import CapacityExceeded, ScopeViolation
from app.domain.scope import TenantContext
from app.mcp.catalogue import CONTRACTS
from app.mcp.telemetry import McpTelemetry
from app.ports.memory import MemorySelectionPort
from app.security.authorization import require_snapshot
from app.services.context_assembly import ContextAssemblyService
from app.services.feature_trace import FeatureTraceService
from app.services.findings_query import FindingsQueryService
from app.services.hybrid_search import HybridSearchService
from app.services.index_jobs import IndexJobService
from app.services.investigation_queries import InvestigationQueryService
from app.services.lexical_search import LexicalSearchService
from app.services.source_view import SourceViewService


class ReadOnlyMcpServer:
    """Strictly read-only local MCP dispatch facade and tool provider."""

    MAX_REQUEST_BYTES = 65536
    MAX_RESPONSE_BYTES = 262144

    SERVER_NAME = "codespy-retrieval-mcp"
    SERVER_VERSION = "1.0.0"

    def __init__(
        self,
        *,
        identity: TenantContext | None = None,
        source_view_service: SourceViewService | None = None,
        lexical_service: LexicalSearchService | None = None,
        hybrid_service: HybridSearchService | None = None,
        context_service: ContextAssemblyService | None = None,
        memory_service: MemorySelectionPort | None = None,
        index_service: IndexJobService | None = None,
        investigation_query_service: InvestigationQueryService | None = None,
        findings_service: FindingsQueryService | None = None,
        feature_trace_service: FeatureTraceService | None = None,
        db_factory: async_sessionmaker[AsyncSession] | None = None,
        repository_allowlist: frozenset[UUID] | None = None,
        telemetry: McpTelemetry | None = None,
    ) -> None:
        self._identity = identity
        self.source_view_service = source_view_service
        self.lexical_service = lexical_service
        self.hybrid_service = hybrid_service
        self.context_service = context_service
        self.memory_service = memory_service
        self.index_service = index_service
        self.investigation_query_service = investigation_query_service
        self.findings_service = findings_service
        self.feature_trace_service = feature_trace_service
        self.db_factory = db_factory
        self.repository_allowlist = repository_allowlist
        self.telemetry = telemetry

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return MCP tool schemas for all 13 read-only capabilities."""
        return [
            {
                "name": "get_index_status",
                "description": "Read a scoped indexing job and its durable publication receipt.",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "repository_id": {"type": "string", "format": "uuid"},
                        "snapshot_id": {"type": "string", "format": "uuid"},
                        "job_id": {"type": "string", "format": "uuid"},
                    },
                    "required": ["repository_id", "snapshot_id", "job_id"],
                },
            },
            {
                "name": "get_source_view",
                "description": (
                    "Fetch model-safe redacted source code for a verified repository file."
                ),
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "repository_id": {"type": "string", "format": "uuid"},
                        "snapshot_id": {"type": "string", "format": "uuid"},
                        "file_id": {"type": "string", "format": "uuid"},
                        "start_byte": {"type": "integer", "minimum": 0},
                        "end_byte": {"type": "integer", "minimum": 0},
                    },
                    "required": ["repository_id", "snapshot_id", "file_id"],
                },
            },
            {
                "name": "search_exact",
                "description": "Execute exact symbol or path search across indexed code chunks.",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "repository_id": {"type": "string", "format": "uuid"},
                        "snapshot_id": {"type": "string", "format": "uuid"},
                        "chunk_set_id": {"type": "string", "format": "uuid"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                    },
                    "required": ["repository_id", "snapshot_id", "query"],
                },
            },
            {
                "name": "search_hybrid",
                "description": (
                    "Execute reciprocal rank fused hybrid search (exact + lexical + dense)."
                ),
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "repository_id": {"type": "string", "format": "uuid"},
                        "snapshot_id": {"type": "string", "format": "uuid"},
                        "chunk_set_id": {"type": "string", "format": "uuid"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                    },
                    "required": ["repository_id", "snapshot_id", "query"],
                },
            },
            {
                "name": "assemble_context",
                "description": "Assemble token-budgeted, citation-keyed grounding context packet.",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "repository_id": {"type": "string", "format": "uuid"},
                        "snapshot_id": {"type": "string", "format": "uuid"},
                        "chunk_set_id": {"type": "string", "format": "uuid"},
                        "query": {"type": "string"},
                        "max_tokens": {
                            "type": "integer",
                            "minimum": 128,
                            "maximum": 32768,
                            "default": 4096,
                        },
                    },
                    "required": ["repository_id", "snapshot_id", "query"],
                },
            },
            {
                "name": "query_memory",
                "description": (
                    "Query evidence-backed repository memories, triage decisions, and findings."
                ),
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "repository_id": {"type": "string", "format": "uuid"},
                        "query": {"type": "string"},
                        "topic": {"type": "string"},
                        "snapshot_id": {"type": "string", "format": "uuid"},
                        "history_mode": {
                            "type": "string",
                            "enum": ["off", "current_only", "historical_hints"],
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                    },
                    "required": ["repository_id", "query", "snapshot_id"],
                },
            },
            {
                "name": "get_investigation_status",
                "description": (
                    "Fetch bounded investigation lifecycle status, job progress, and verdict."
                ),
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "repository_id": {"type": "string", "format": "uuid"},
                        "investigation_id": {"type": "string", "format": "uuid"},
                    },
                    "required": ["repository_id", "investigation_id"],
                },
            },
            {
                "name": "get_investigation_result",
                "description": (
                    "Fetch final report, verified claims, and evidence count for an investigation."
                ),
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "repository_id": {"type": "string", "format": "uuid"},
                        "investigation_id": {"type": "string", "format": "uuid"},
                    },
                    "required": ["repository_id", "investigation_id"],
                },
            },
            {
                "name": "list_findings",
                "description": (
                    "List authorized findings for a scan run with current support filtering."
                ),
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "repository_id": {"type": "string", "format": "uuid"},
                        "snapshot_id": {"type": "string", "format": "uuid"},
                        "run_id": {"type": "string", "format": "uuid"},
                        "category": {"type": "string"},
                        "severity": {"type": "string"},
                        "disposition": {"type": "string"},
                        "triage_state": {"type": "string"},
                        "cursor": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                    },
                    "required": ["repository_id", "snapshot_id", "run_id"],
                },
            },
            {
                "name": "get_evidence",
                "description": (
                    "Fetch a scoped, safe evidence envelope and permitted source reference."
                ),
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "repository_id": {"type": "string", "format": "uuid"},
                        "snapshot_id": {"type": "string", "format": "uuid"},
                        "evidence_id": {"type": "string", "format": "uuid"},
                        "run_id": {"type": "string", "format": "uuid"},
                    },
                    "required": ["repository_id", "snapshot_id", "evidence_id"],
                },
            },
            {
                "name": "graph_neighbors",
                "description": (
                    "Traverse neighboring nodes and edges within one published structural run."
                ),
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "repository_id": {"type": "string", "format": "uuid"},
                        "snapshot_id": {"type": "string", "format": "uuid"},
                        "node_id": {"type": "string", "format": "uuid"},
                        "structural_run_id": {"type": "string", "format": "uuid"},
                        "max_hops": {"type": "integer", "minimum": 1, "maximum": 2, "default": 1},
                        "max_nodes": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 50,
                            "default": 20,
                        },
                        "max_edges": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 100,
                            "default": 40,
                        },
                    },
                    "required": ["repository_id", "snapshot_id", "node_id"],
                },
            },
            {
                "name": "trace_feature",
                "description": (
                    "Trace feature code graph neighborhood rooted at a specific finding."
                ),
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "repository_id": {"type": "string", "format": "uuid"},
                        "snapshot_id": {"type": "string", "format": "uuid"},
                        "finding_id": {"type": "string", "format": "uuid"},
                        "max_hops": {"type": "integer", "minimum": 0, "maximum": 2, "default": 2},
                        "max_nodes": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 50,
                        },
                    },
                    "required": ["repository_id", "snapshot_id", "finding_id"],
                },
            },
            {
                "name": "list_claims",
                "description": (
                    "List verified and unverified claims from an authorized investigation report."
                ),
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "repository_id": {"type": "string", "format": "uuid"},
                        "investigation_id": {"type": "string", "format": "uuid"},
                    },
                    "required": ["repository_id", "investigation_id"],
                },
            },
        ]

    async def handle_request(self, req: dict[str, Any]) -> dict[str, Any]:
        """Process incoming JSON-RPC 2.0 MCP request and route to read-only tool handler."""
        if not self._valid_envelope(req):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request"},
            }
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": self.SERVER_NAME,
                        "version": self.SERVER_VERSION,
                    },
                },
            }

        if method == "notifications/initialized":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": self.get_tool_definitions()},
            }

        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            try:
                if (
                    set(params) - {"name", "arguments"}
                    or not isinstance(tool_name, str)
                    or not isinstance(arguments, dict)
                ):
                    raise ValueError("Invalid tool parameters")
                content_text = await self._dispatch_tool(tool_name, arguments)
                response = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": content_text}],
                        "isError": False,
                    },
                }
                if (
                    len(json.dumps(response, ensure_ascii=True).encode("utf-8"))
                    > self.MAX_RESPONSE_BYTES
                ):
                    return self._tool_error(req_id, "Tool result exceeds output limit")
                return response
            except ScopeViolation:
                return self._tool_error(req_id, "Security/Scope Violation")
            except (ValueError, KeyError, TypeError):
                return self._tool_error(req_id, "Invalid Parameters")
            except Exception:
                return self._tool_error(req_id, "Tool Execution Error")

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": "Method not found"},
        }

    @classmethod
    def _valid_envelope(cls, req: object) -> bool:
        if not isinstance(req, dict) or set(req) - {"jsonrpc", "id", "method", "params"}:
            return False
        request_id = req.get("id")
        if (
            req.get("jsonrpc") != "2.0"
            or not isinstance(req.get("method"), str)
            or not isinstance(req.get("params", {}), dict)
            or isinstance(request_id, bool)
            or (request_id is not None and not isinstance(request_id, (str, int)))
            or (isinstance(request_id, str) and len(request_id) > 128)
            or (isinstance(request_id, int) and request_id.bit_length() > 64)
            or ("id" not in req and req.get("method") != "notifications/initialized")
        ):
            return False
        try:
            return len(json.dumps(req, allow_nan=False).encode("utf-8")) <= cls.MAX_REQUEST_BYTES
        except (TypeError, ValueError, RecursionError):
            return False

    @staticmethod
    def _tool_error(request_id: object, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": message}], "isError": True},
        }

    async def dispatch_tool(self, name: str, args: dict[str, Any]) -> str:
        """Public entrypoint for tool execution using typed contracts."""
        return await self._dispatch_tool(name, args)

    async def _dispatch_tool(self, name: str, args: dict[str, Any]) -> str:
        """Route tool call to read-only service. Mutating tools are strictly forbidden."""
        definitions = {tool["name"]: tool["inputSchema"] for tool in self.get_tool_definitions()}
        if not isinstance(name, str) or name not in definitions:
            raise ValueError("Unknown or unauthorized tool")
        if self._identity is None:
            raise ScopeViolation("Trusted MCP identity is required")
        if "org_id" in args or "principal_id" in args or "role" in args:
            raise ScopeViolation("Tool arguments cannot select an identity")

        # Repository allowlist check
        if self.repository_allowlist is not None and "repository_id" in args:
            try:
                repo_check_uuid = UUID(args["repository_id"])
                if repo_check_uuid not in self.repository_allowlist:
                    raise ScopeViolation(
                        f"Repository {repo_check_uuid} not permitted by server configuration"
                    )
            except (ValueError, TypeError):
                raise ValueError("Invalid repository_id UUID") from None

        args = (
            CONTRACTS[name][0]
            .model_validate(args)
            .model_dump(mode="json", exclude_none=True, exclude_unset=True)
        )

        if name == "get_index_status":
            if self.index_service is None:
                raise RuntimeError("IndexJobService not configured on MCP server")
            GetIndexStatusInputV1.model_validate(args)
            status = await self.index_service.get(
                self._identity,
                UUID(args["repository_id"]),
                UUID(args["snapshot_id"]),
                UUID(args["job_id"]),
            )
            return json.dumps(status.model_dump(mode="json"))

        if name == "get_source_view":
            if not self.source_view_service:
                raise RuntimeError("SourceViewService not configured on MCP server")
            GetSourceViewInputV1.model_validate(args)
            byte_range = None
            if "start_byte" in args and "end_byte" in args:
                byte_range = ByteRangeV1(start_byte=args["start_byte"], end_byte=args["end_byte"])
            req = SourceViewRequestV1(
                org_id=self._identity.org_id,
                repository_id=UUID(args["repository_id"]),
                snapshot_id=UUID(args["snapshot_id"]),
                principal_id=self._identity.principal_id,
                file_id=UUID(args["file_id"]),
                byte_range=byte_range,
                representation=SourceRepresentation.MODEL_SAFE,
            )
            res = await self.source_view_service.get(req, PrivacyPolicyV1())
            return json.dumps(res.model_dump(mode="json"))

        if name == "search_exact":
            if not self.lexical_service:
                raise RuntimeError("LexicalSearchService not configured on MCP server")
            SearchExactInputV1.model_validate(args)
            scope = SearchScopeV1(
                org_id=self._identity.org_id,
                repository_id=UUID(args["repository_id"]),
                snapshot_id=UUID(args["snapshot_id"]),
                principal_id=self._identity.principal_id,
                chunk_set_id=UUID(args["chunk_set_id"]) if "chunk_set_id" in args else None,
            )
            req_exact = ExactSearchRequestV1(
                scope=scope,
                query=str(args["query"]),
                limit=int(args.get("limit", 5)),
            )
            res_exact = await self.lexical_service.search_exact(req_exact)
            return json.dumps(res_exact.model_dump(mode="json"))

        if name == "search_hybrid":
            if not self.hybrid_service:
                raise RuntimeError("HybridSearchService not configured on MCP server")
            SearchHybridInputV1.model_validate(args)
            scope = SearchScopeV1(
                org_id=self._identity.org_id,
                repository_id=UUID(args["repository_id"]),
                snapshot_id=UUID(args["snapshot_id"]),
                principal_id=self._identity.principal_id,
                chunk_set_id=UUID(args["chunk_set_id"]) if "chunk_set_id" in args else None,
            )
            req_hybrid = HybridSearchRequestV1(
                scope=scope,
                query=str(args["query"]),
                limit=int(args.get("limit", 5)),
            )
            res_hybrid = await self.hybrid_service.search_hybrid(req_hybrid)
            res_dict = res_hybrid.model_dump(mode="json")
            if not getattr(self.hybrid_service, "dense_search", None):
                res_dict["dense_coverage"] = "unavailable"
                res_dict["fallback_mode"] = "lexical_only"
            return json.dumps(res_dict)

        if name == "assemble_context":
            if not self.context_service or not self.hybrid_service:
                raise RuntimeError("Context / Hybrid service not configured on MCP server")
            AssembleContextInputV1.model_validate(args)
            scope = SearchScopeV1(
                org_id=self._identity.org_id,
                repository_id=UUID(args["repository_id"]),
                snapshot_id=UUID(args["snapshot_id"]),
                principal_id=self._identity.principal_id,
                chunk_set_id=UUID(args["chunk_set_id"]) if "chunk_set_id" in args else None,
            )
            h_res = await self.hybrid_service.search_hybrid(
                HybridSearchRequestV1(scope=scope, query=str(args["query"]), limit=20)
            )
            max_tok = int(args.get("max_tokens", 4096))
            c_res = await self.context_service.assemble_context(
                ContextAssemblyRequestV1(
                    scope=h_res.scope,
                    query=str(args["query"]),
                    candidate_hits=h_res.hits,
                    budget=ContextBudgetV1(max_tokens=max_tok),
                )
            )
            return json.dumps(c_res.model_dump(mode="json"))

        if name == "query_memory":
            if not self.memory_service:
                raise RuntimeError("MemoryService not configured on MCP server")
            QueryMemoryInputV1.model_validate(args)
            m_req = MemoryQueryRequestV1(
                org_id=self._identity.org_id,
                repository_id=UUID(args["repository_id"]),
                principal_id=self._identity.principal_id,
                query=str(args["query"]),
                topic=args.get("topic"),
                snapshot_id=UUID(args["snapshot_id"]) if "snapshot_id" in args else None,
                limit=int(args.get("limit", 5)),
                history_mode=args.get("history_mode", "current_only"),
            )
            m_res = await self.memory_service.query_memory(m_req)
            return json.dumps(m_res.model_dump(mode="json"))

        if name == "get_investigation_status":
            if not self.investigation_query_service:
                raise RuntimeError("InvestigationQueryService not configured on MCP server")
            GetInvestigationStatusInputV1.model_validate(args)
            detail = await self.investigation_query_service.detail(
                self._identity,
                UUID(args["repository_id"]),
                UUID(args["investigation_id"]),
            )
            status_payload = {
                "current_support_available": detail.current_support_available,
                "investigation_id": str(detail.summary.id),
                "repository_id": str(detail.summary.repository_id),
                "snapshot_id": (
                    str(detail.summary.snapshot_id) if detail.summary.snapshot_id else None
                ),
                "status": detail.summary.status,
                "target_type": detail.summary.target_type,
                "query": detail.summary.query,
                "current_step": detail.summary.current_step,
                "max_steps": detail.summary.max_steps,
                "budget_limit_micros": detail.summary.budget_limit_micros,
                "spent_micros": detail.summary.spent_micros,
                "held_micros": detail.summary.held_micros,
                "verdict": detail.summary.verdict,
                "verdict_confidence": detail.summary.verdict_confidence,
                "summary": detail.summary.summary,
                "failure_reason": detail.summary.failure_reason,
                "available_actions": list(detail.available_actions),
                "claims_count": detail.claims_count,
                "evidence_count": detail.evidence_count,
                "events_count": detail.events_count,
                "job": (
                    {
                        "id": str(detail.job.id),
                        "state": detail.job.state,
                        "attempt_count": detail.job.attempt_count,
                        "max_attempts": detail.job.max_attempts,
                    }
                    if detail.job
                    else None
                ),
            }
            return json.dumps(status_payload)

        if name == "get_investigation_result":
            if not self.investigation_query_service:
                raise RuntimeError("InvestigationQueryService not configured on MCP server")
            GetInvestigationResultInputV1.model_validate(args)
            detail = await self.investigation_query_service.detail(
                self._identity,
                UUID(args["repository_id"]),
                UUID(args["investigation_id"]),
            )
            result_payload = {
                "snapshot_id": str(detail.summary.snapshot_id)
                if detail.summary.snapshot_id
                else None,
                "current_support_available": detail.current_support_available,
                "investigation_id": str(detail.summary.id),
                "repository_id": str(detail.summary.repository_id),
                "status": detail.summary.status,
                "verdict": detail.summary.verdict,
                "verdict_confidence": detail.summary.verdict_confidence,
                "summary": detail.summary.summary,
                "claims_count": detail.claims_count,
                "evidence_count": detail.evidence_count,
                "report": detail.report,
            }
            return json.dumps(result_payload)

        if name == "list_findings":
            if not self.findings_service:
                raise RuntimeError("FindingsQueryService not configured on MCP server")
            ListFindingsInputV1.model_validate(args)
            list_res = await self.findings_service.list_findings(
                self._identity,
                UUID(args["repository_id"]),
                UUID(args["snapshot_id"]),
                UUID(args["run_id"]),
                category=args.get("category"),
                severity=args.get("severity"),
                disposition=args.get("disposition"),
                triage_state=args.get("triage_state"),
                cursor=args.get("cursor"),
                limit=int(args.get("limit", 20)),
            )
            findings_dto = tuple(
                McpFindingItemV1(
                    id=f.id,
                    rule_id=f.rule_id,
                    category=f.category,
                    severity=f.severity,
                    disposition=f.disposition,
                    current_support=f.support_reason,
                    title=f.title,
                    file_path=f.file_path,
                    line=f.line_number,
                    triage_state=str(f.triage_state),
                )
                for f in list_res.items
            )
            out_res = ListFindingsResultV1(
                repository_id=UUID(args["repository_id"]),
                snapshot_id=UUID(args["snapshot_id"]),
                run_id=UUID(args["run_id"]),
                findings=findings_dto,
                next_cursor=list_res.next_cursor,
                total_count=list_res.total_count,
            )
            return json.dumps(out_res.model_dump(mode="json"))

        if name == "get_evidence":
            if not self.db_factory:
                raise RuntimeError("Database session factory not configured on MCP server")
            GetEvidenceInputV1.model_validate(args)
            repo_id = UUID(args["repository_id"])
            snap_id = UUID(args["snapshot_id"])
            ev_id = UUID(args["evidence_id"])
            run_id = UUID(args["run_id"]) if args.get("run_id") else None

            async with self.db_factory() as session:
                await require_snapshot(session, self._identity, repo_id, snap_id)
                from app.models.tables import evidence_records, source_files

                stmt = (
                    select(evidence_records, source_files.c.path.label("file_path"))
                    .select_from(
                        evidence_records.outerjoin(
                            source_files,
                            (source_files.c.org_id == evidence_records.c.org_id)
                            & (source_files.c.snapshot_id == evidence_records.c.snapshot_id)
                            & (source_files.c.id == evidence_records.c.file_id),
                        )
                    )
                    .where(
                        evidence_records.c.org_id == self._identity.org_id,
                        evidence_records.c.snapshot_id == snap_id,
                        evidence_records.c.id == ev_id,
                    )
                )
                if run_id is not None:
                    stmt = stmt.where(evidence_records.c.run_id == run_id)
                row = (await session.execute(stmt)).mappings().one_or_none()
                if row is None:
                    raise ScopeViolation(f"Evidence {ev_id} not found in repository/snapshot scope")

                from app.domain.scope import RunRef
                from app.repositories.analysis_repository import AnalysisRepository

                await AnalysisRepository(session).get(
                    self._identity,
                    RunRef(org_id=self._identity.org_id, snapshot_id=snap_id, run_id=row["run_id"]),
                    published=True,
                )
                range_dict = None
                if row["start_line"] is not None:
                    range_dict = {
                        "start_line": row["start_line"],
                        "start_col": row["start_col"],
                        "end_line": row["end_line"],
                        "end_col": row["end_col"],
                    }
                ev_res = McpEvidenceResultV1(
                    provider_name=row["provider_name"],
                    provider_version=row["provider_version"],
                    operation=row["operation"],
                    evidence_digest=row["digest"],
                    id=row["id"],
                    repository_id=repo_id,
                    snapshot_id=snap_id,
                    run_id=row["run_id"],
                    file_id=row["file_id"],
                    file_path=row["file_path"],
                    range=range_dict,
                    observation_mode=row["observation_mode"],
                    basis=row["basis"],
                    coverage_status=row["coverage_status"],
                    payload={},
                    limitations=(*tuple(row["limitations"] or ()), "raw_payload_omitted"),
                    captured_at=row["captured_at"].isoformat() if row.get("captured_at") else None,
                )
                return json.dumps(ev_res.model_dump(mode="json"))

        if name == "graph_neighbors":
            if not self.db_factory:
                raise RuntimeError("Database session factory not configured on MCP server")
            GraphNeighborsInputV1.model_validate(args)
            repo_id = UUID(args["repository_id"])
            snap_id = UUID(args["snapshot_id"])
            node_id = UUID(args["node_id"])
            structural_run_id = (
                UUID(args["structural_run_id"]) if args.get("structural_run_id") else None
            )
            max_hops = int(args.get("max_hops", 1))
            max_nodes = int(args.get("max_nodes", 20))
            max_edges = int(args.get("max_edges", 40))

            async with self.db_factory() as session:
                await require_snapshot(session, self._identity, repo_id, snap_id)
                from app.models.phase3_tables import structural_reports
                from app.models.tables import cpg_edges, cpg_nodes

                if structural_run_id is None:
                    run_stmt = (
                        select(structural_reports.c.run_id)
                        .where(
                            structural_reports.c.org_id == self._identity.org_id,
                            structural_reports.c.snapshot_id == snap_id,
                        )
                        .order_by(structural_reports.c.created_at.desc())
                        .limit(1)
                    )
                    structural_run_id = (await session.execute(run_stmt)).scalar_one_or_none()
                    if structural_run_id is None:
                        raise ScopeViolation("No published structural run found for snapshot")

                from app.domain.scope import RunRef
                from app.repositories.analysis_repository import AnalysisRepository

                await AnalysisRepository(session).get(
                    self._identity,
                    RunRef(
                        org_id=self._identity.org_id, snapshot_id=snap_id, run_id=structural_run_id
                    ),
                    published=True,
                )
                root_stmt = select(cpg_nodes).where(
                    cpg_nodes.c.org_id == self._identity.org_id,
                    cpg_nodes.c.snapshot_id == snap_id,
                    cpg_nodes.c.run_id == structural_run_id,
                    cpg_nodes.c.local_id == node_id,
                )
                root_row = (await session.execute(root_stmt)).mappings().one_or_none()
                if root_row is None:
                    raise ScopeViolation(
                        f"Root node {node_id} not found in structural run {structural_run_id}"
                    )

                visited_nodes = {node_id: root_row}
                collected_edges: list[Any] = []
                seen_edges: set[UUID] = set()
                current_frontier = {node_id}
                omissions: list[str] = []
                actual_hops: int = 0

                for hop_index in range(1, max_hops + 1):
                    if not current_frontier or len(visited_nodes) >= max_nodes:
                        break
                    actual_hops = hop_index
                    edge_stmt = (
                        select(cpg_edges)
                        .where(
                            cpg_edges.c.org_id == self._identity.org_id,
                            cpg_edges.c.snapshot_id == snap_id,
                            cpg_edges.c.run_id == structural_run_id,
                            or_(
                                cpg_edges.c.source_local_id.in_(current_frontier),
                                cpg_edges.c.target_local_id.in_(current_frontier),
                            ),
                        )
                        .order_by(cpg_edges.c.id)
                        .limit(max_edges - len(collected_edges) + 1)
                    )
                    edge_rows = (await session.execute(edge_stmt)).mappings().all()
                    next_frontier: set[UUID] = set()
                    for erow in edge_rows:
                        if erow["id"] in seen_edges:
                            continue
                        if len(collected_edges) >= max_edges:
                            omissions.append("max_edges_exceeded")
                            break
                        collected_edges.append(erow)
                        seen_edges.add(erow["id"])
                        neighbor_id = (
                            erow["target_local_id"]
                            if erow["source_local_id"] in current_frontier
                            else erow["source_local_id"]
                        )
                        if (
                            neighbor_id not in visited_nodes
                            and len(visited_nodes) + len(next_frontier) < max_nodes
                        ):
                            next_frontier.add(neighbor_id)
                        elif neighbor_id not in visited_nodes:
                            omissions.append("max_nodes_exceeded")
                    if next_frontier:
                        n_stmt = select(cpg_nodes).where(
                            cpg_nodes.c.org_id == self._identity.org_id,
                            cpg_nodes.c.snapshot_id == snap_id,
                            cpg_nodes.c.run_id == structural_run_id,
                            cpg_nodes.c.local_id.in_(next_frontier),
                        )
                        for nrow in (
                            (await session.execute(n_stmt.order_by(cpg_nodes.c.local_id)))
                            .mappings()
                            .all()
                        ):
                            visited_nodes[nrow["local_id"]] = nrow
                    current_frontier = next_frontier

                if current_frontier:
                    omissions.append("bounded_relationship_coverage")
                visible_edges = [
                    e
                    for e in collected_edges
                    if e["source_local_id"] in visited_nodes
                    and e["target_local_id"] in visited_nodes
                ]
                if len(visible_edges) != len(collected_edges):
                    omissions.append("unresolved_neighbor_omitted")
                nodes_dto = tuple(
                    McpGraphNodeV1(
                        file_id=r["file_id"],
                        id=r["local_id"],
                        kind=r["node_kind"],
                        label=r["symbol_name"],
                        file_path=r.get("file_path"),
                        line=r.get("start_line"),
                        properties={},
                    )
                    for r in visited_nodes.values()
                )
                edges_dto = tuple(
                    McpGraphEdgeV1(
                        source_id=e["source_local_id"],
                        target_id=e["target_local_id"],
                        edge_kind=e["edge_kind"],
                        properties={},
                    )
                    for e in visible_edges
                )
                result_dto = GraphNeighborsResultV1(
                    repository_id=repo_id,
                    snapshot_id=snap_id,
                    structural_run_id=structural_run_id,
                    root_node_id=node_id,
                    nodes=nodes_dto,
                    edges=edges_dto,
                    hop_count=actual_hops,
                    omission_reasons=tuple(sorted(set(omissions))),
                )
                return json.dumps(result_dto.model_dump(mode="json"))

        if name == "trace_feature":
            if not self.feature_trace_service:
                raise RuntimeError("FeatureTraceService not configured on MCP server")
            TraceFeatureInputV1.model_validate(args)
            budget = TraceBudgetV1(
                max_hops=int(args.get("max_hops", 2)),
                max_nodes=int(args.get("max_nodes", 50)),
            )
            trace_res = await self.feature_trace_service.trace(
                self._identity,
                UUID(args["repository_id"]),
                UUID(args["snapshot_id"]),
                UUID(args["finding_id"]),
                budget=budget,
            )
            return json.dumps(trace_res.model_dump(mode="json"))

        if name == "list_claims":
            if not self.investigation_query_service:
                raise RuntimeError("InvestigationQueryService not configured on MCP server")
            ListClaimsInputV1.model_validate(args)
            detail = await self.investigation_query_service.detail(
                self._identity,
                UUID(args["repository_id"]),
                UUID(args["investigation_id"]),
            )
            report = detail.report or {}
            claims_raw = report.get("claims", [])
            verifs_raw = {
                str(v.get("claim_id")): v
                for v in report.get("verifications", [])
                if isinstance(v, dict) and "claim_id" in v
            }
            claims_dto = []
            for c in claims_raw:
                cid = UUID(str(c["claim_id"]))
                v = verifs_raw.get(str(cid))
                claims_dto.append(
                    McpClaimItemV1(
                        claim_id=cid,
                        statement=c.get("statement", ""),
                        kind=c.get("kind", ""),
                        status=v.get("status", "unverified") if v else "unverified",
                        evidence_refs=tuple(UUID(str(e)) for e in c.get("evidence_ids", ())),
                        resolved_file=v.get("resolved_file") if v else None,
                        resolved_line_start=v.get("resolved_line_start") if v else None,
                        resolved_line_end=v.get("resolved_line_end") if v else None,
                        notes=v.get("notes", "") if v else "",
                    )
                )
            claims_res = ListClaimsResultV1(
                snapshot_id=detail.summary.snapshot_id,
                current_support_available=detail.current_support_available,
                repository_id=UUID(args["repository_id"]),
                investigation_id=UUID(args["investigation_id"]),
                claims=tuple(claims_dto),
                total_count=len(claims_dto),
            )
            return json.dumps(claims_res.model_dump(mode="json"))

        raise ValueError("Unknown or unauthorized tool")


class BoundedLineReader:
    """Bounded line reader that rejects frames exceeding MAX_FRAME_BYTES before deserialization."""

    MAX_FRAME_BYTES: int = 65536

    def __init__(self, raw_stream: Any, max_bytes: int = 65536) -> None:
        self.raw_stream = raw_stream
        self.max_bytes = max_bytes
        self._buffer: bytes = b""

    async def readline(self) -> str:
        while b"\n" not in self._buffer:
            chunk = await self.raw_stream.read(4096)
            if not chunk:
                line = self._buffer
                self._buffer = b""
                if len(line) > self.max_bytes:
                    raise ValueError(f"Incoming frame exceeds {self.max_bytes} bytes limit")
                return line.decode("utf-8", errors="strict")
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            self._buffer += chunk
            if len(self._buffer) > self.max_bytes and b"\n" not in self._buffer:
                self._buffer = b""
                raise ValueError(f"Incoming frame exceeds {self.max_bytes} bytes limit")

        idx = self._buffer.index(b"\n")
        line = self._buffer[: idx + 1]
        self._buffer = self._buffer[idx + 1 :]
        if len(line) > self.max_bytes:
            raise ValueError(f"Incoming frame exceeds {self.max_bytes} bytes limit")
        return line.decode("utf-8", errors="strict")

    async def __aiter__(self) -> AsyncIterator[str]:
        while True:
            line = await self.readline()
            if not line:
                break
            yield line


class McpConcurrencyLimiter:
    """Enforces concurrency boundaries (2 active calls, 4 queue slots, 30s deadline)."""

    def __init__(
        self,
        max_active: int = 2,
        max_waiting: int = 4,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.max_active = max_active
        self.max_waiting = max_waiting
        self.timeout_seconds = timeout_seconds
        self._active = 0
        self._waiting = 0
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max_active)

    @asynccontextmanager
    async def throttle(self) -> AsyncGenerator[None, None]:
        queued = False
        acquired = False
        async with self._lock:
            # Semaphore value includes reservations not yet reflected in _active.
            if self._sem.locked():
                if self._waiting >= self.max_waiting:
                    raise CapacityExceeded(
                        "MCP server capacity exceeded: active/waiting queue full"
                    )
                self._waiting += 1
                queued = True
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self._sem.acquire()
                acquired = True
                if queued:
                    self._waiting -= 1
                    queued = False
                self._active += 1
                yield
        finally:
            # No awaits: cancellation cannot interrupt release and leak capacity.
            if queued:
                self._waiting -= 1
            if acquired:
                self._active -= 1
                self._sem.release()


def _extract_omissions_count(content_text: str) -> int:
    """Safely extract count of omissions from JSON payload without inspecting raw details."""
    try:
        data = json.loads(content_text)
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(data, dict):
        return 0
    omissions = data.get("omissions")
    if isinstance(omissions, list):
        return len(omissions)
    if isinstance(omissions, int):
        return omissions
    omitted_count = data.get("omitted_count")
    if isinstance(omitted_count, int):
        return omitted_count
    if "total_count" in data and "items" in data and isinstance(data.get("items"), list):
        total = data.get("total_count")
        return max(0, total - len(data["items"])) if isinstance(total, int) else 0
    return 0


def create_mcp_server(
    facade: ReadOnlyMcpServer,
    limiter: McpConcurrencyLimiter | None = None,
    telemetry: McpTelemetry | None = None,
) -> Server:
    """Create an official MCP SDK Server wired to the dispatch facade."""
    server = Server(name=facade.SERVER_NAME, version=facade.SERVER_VERSION)
    server.middleware.clear()  # Disable default OpenTelemetry to prevent unconfigured exports
    rate_limiter = limiter or McpConcurrencyLimiter()
    tel = telemetry or facade.telemetry or McpTelemetry()

    async def list_tools_handler(
        _ctx: ServerRequestContext[Any, Any],
        _req: types.PaginatedRequestParams,
    ) -> types.ListToolsResult:
        tools = [
            types.Tool(
                name=t["name"],
                description=t["description"],
                input_schema=CONTRACTS[t["name"]][0].model_json_schema(),
                output_schema=CONTRACTS[t["name"]][1].model_json_schema(),
                annotations=types.ToolAnnotations(
                    read_only_hint=True,
                    destructive_hint=False,
                    idempotent_hint=True,
                    open_world_hint=False,
                ),
            )
            for t in facade.get_tool_definitions()
        ]
        return types.ListToolsResult(tools=tools)

    async def call_tool_handler(
        _ctx: ServerRequestContext[Any, Any],
        req: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        corr_id = str(uuid.uuid4())
        tool_name = req.name if req.name in CONTRACTS else "unknown"
        t0 = time.perf_counter()
        queue_wait_ms = 0.0
        try:
            throttle_start = time.perf_counter()
            async with rate_limiter.throttle():
                queue_wait_ms = (time.perf_counter() - throttle_start) * 1000.0
                content_text = await facade.dispatch_tool(req.name, req.arguments or {})
                duration_ms = (time.perf_counter() - t0) * 1000.0
                resp_bytes = len(content_text.encode("utf-8"))
                oversized = resp_bytes > facade.MAX_RESPONSE_BYTES
                if not oversized:
                    output = CONTRACTS[req.name][1].model_validate_json(content_text)
                    data = output.model_dump(mode="json")
                    result = types.CallToolResult(
                        content=[types.TextContent(type="text", text=json.dumps(data))],
                        structured_content=data,
                        is_error=False,
                    )
                    # Reserve the maximum input-frame size for request IDs and SDK metadata.
                    resp_bytes = len(result.model_dump_json(by_alias=True).encode("utf-8"))
                    oversized = resp_bytes + 65536 > facade.MAX_RESPONSE_BYTES
                if oversized:
                    tel.record_call(
                        correlation_id=corr_id,
                        tool_name=tool_name,
                        outcome_category="size_exceeded",
                        duration_ms=duration_ms,
                        queue_wait_ms=queue_wait_ms,
                        response_bytes=resp_bytes,
                        coverage_omissions_count=0,
                    )
                    return types.CallToolResult(
                        content=[
                            types.TextContent(
                                type="text",
                                text="Tool result exceeds maximum allowed size (256 KiB)",
                            )
                        ],
                        structured_content=McpErrorV1(
                            code="response_too_large",
                            message="Reduce the requested result size",
                        ).model_dump(exclude_none=True),
                        is_error=True,
                    )
                omissions_count = _extract_omissions_count(content_text)
                tel.record_call(
                    correlation_id=corr_id,
                    tool_name=tool_name,
                    outcome_category="success",
                    duration_ms=duration_ms,
                    queue_wait_ms=queue_wait_ms,
                    response_bytes=resp_bytes,
                    coverage_omissions_count=omissions_count,
                )
                return result
        except CapacityExceeded:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            tel.record_call(
                correlation_id=corr_id,
                tool_name=tool_name,
                outcome_category="capacity_exceeded",
                duration_ms=duration_ms,
                queue_wait_ms=queue_wait_ms,
                response_bytes=0,
                coverage_omissions_count=0,
            )
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="Server Busy / Capacity Exceeded")],
                structured_content=McpErrorV1(code="capacity_exceeded").model_dump(
                    exclude_none=True
                ),
                is_error=True,
            )
        except TimeoutError:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            tel.record_call(
                correlation_id=corr_id,
                tool_name=tool_name,
                outcome_category="timeout",
                duration_ms=duration_ms,
                queue_wait_ms=queue_wait_ms,
                response_bytes=0,
                coverage_omissions_count=0,
            )
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text="Tool execution exceeded 30s deadline")
                ],
                structured_content=McpErrorV1(code="timeout").model_dump(exclude_none=True),
                is_error=True,
            )
        except asyncio.CancelledError:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            tel.record_call(
                correlation_id=corr_id,
                tool_name=tool_name,
                outcome_category="cancellation",
                duration_ms=duration_ms,
                queue_wait_ms=queue_wait_ms,
                response_bytes=0,
                coverage_omissions_count=0,
            )
            raise
        except ScopeViolation:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            tel.record_call(
                correlation_id=corr_id,
                tool_name=tool_name,
                outcome_category="scope_violation",
                duration_ms=duration_ms,
                queue_wait_ms=queue_wait_ms,
                response_bytes=0,
                coverage_omissions_count=0,
            )
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text="Security/Scope Violation: unavailable under current authorization",
                    )
                ],
                structured_content=McpErrorV1(code="scope_denied").model_dump(exclude_none=True),
                is_error=True,
            )
        except (ValueError, KeyError, TypeError):
            duration_ms = (time.perf_counter() - t0) * 1000.0
            tel.record_call(
                correlation_id=corr_id,
                tool_name=tool_name,
                outcome_category="invalid_parameters",
                duration_ms=duration_ms,
                queue_wait_ms=queue_wait_ms,
                response_bytes=0,
                coverage_omissions_count=0,
            )
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text="Invalid Parameters: request or result violates the tool contract",
                    )
                ],
                structured_content=McpErrorV1(code="invalid_parameters").model_dump(
                    exclude_none=True
                ),
                is_error=True,
            )
        except Exception:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            tel.record_call(
                correlation_id=corr_id,
                tool_name=tool_name,
                outcome_category="error",
                duration_ms=duration_ms,
                queue_wait_ms=queue_wait_ms,
                response_bytes=0,
                coverage_omissions_count=0,
            )
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text", text="Tool Execution Error: capability unavailable"
                    )
                ],
                structured_content=McpErrorV1(code="capability_unavailable").model_dump(
                    exclude_none=True
                ),
                is_error=True,
            )

    server.add_request_handler("tools/list", types.PaginatedRequestParams, list_tools_handler)
    server.add_request_handler("tools/call", types.CallToolRequestParams, call_tool_handler)
    return server
