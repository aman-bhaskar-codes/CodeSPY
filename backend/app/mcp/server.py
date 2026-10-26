"""Read-only in-process MCP-style tool dispatcher with trusted identity injection."""

import json
from typing import Any
from uuid import UUID

from app.contracts.context import ContextAssemblyRequestV1, ContextBudgetV1
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
from app.domain.errors import ScopeViolation
from app.domain.scope import TenantContext
from app.ports.memory import MemorySelectionPort
from app.services.context_assembly import ContextAssemblyService
from app.services.hybrid_search import HybridSearchService
from app.services.index_jobs import IndexJobService
from app.services.investigation_queries import InvestigationQueryService
from app.services.lexical_search import LexicalSearchService
from app.services.source_view import SourceViewService


class ReadOnlyMcpServer:
    """Strictly read-only local MCP server providing source view, retrieval, and context tools."""

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
    ) -> None:
        self._identity = identity
        self.source_view_service = source_view_service
        self.lexical_service = lexical_service
        self.hybrid_service = hybrid_service
        self.context_service = context_service
        self.memory_service = memory_service
        self.index_service = index_service
        self.investigation_query_service = investigation_query_service

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return MCP tool schemas for read-only retrieval capabilities."""
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
                    "required": [
                        "repository_id",
                        "snapshot_id",
                        "file_id",
                    ],
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

    async def _dispatch_tool(self, name: str, args: dict[str, Any]) -> str:
        """Route tool call to read-only service. Mutating tools are strictly forbidden."""
        definitions = {tool["name"]: tool["inputSchema"] for tool in self.get_tool_definitions()}
        if not isinstance(name, str) or name not in definitions:
            raise ValueError("Unknown or unauthorized tool")
        if self._identity is None:
            raise ScopeViolation("Trusted MCP identity is required")
        if "org_id" in args or "principal_id" in args:
            raise ScopeViolation("Tool arguments cannot select an identity")
        schema = definitions[name]
        if set(args) - set(schema["properties"]) or set(schema["required"]) - set(args):
            raise ValueError("Tool arguments do not match the declared schema")

        for key, value in args.items():
            spec = schema["properties"][key]
            if (spec["type"] == "string" and not isinstance(value, str)) or (
                spec["type"] == "integer" and type(value) is not int
            ):
                raise ValueError("Tool argument type mismatch")
            if spec["type"] == "integer" and (
                value < spec.get("minimum", 0) or value > spec.get("maximum", 2**63 - 1)
            ):
                raise ValueError("Tool argument outside bounds")
        if ("start_byte" in args) != ("end_byte" in args):
            raise ValueError("Byte range requires both endpoints")

        if name == "get_index_status":
            if self.index_service is None:
                raise RuntimeError("IndexJobService not configured on MCP server")
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
            return json.dumps(res_hybrid.model_dump(mode="json"))

        if name == "assemble_context":
            if not self.context_service or not self.hybrid_service:
                raise RuntimeError("Context / Hybrid service not configured on MCP server")
            scope = SearchScopeV1(
                org_id=self._identity.org_id,
                repository_id=UUID(args["repository_id"]),
                snapshot_id=UUID(args["snapshot_id"]),
                principal_id=self._identity.principal_id,
                chunk_set_id=UUID(args["chunk_set_id"]) if "chunk_set_id" in args else None,
            )
            # Run hybrid search first to gather candidates
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
            detail = await self.investigation_query_service.detail(
                self._identity,
                UUID(args["repository_id"]),
                UUID(args["investigation_id"]),
            )
            status_payload = {
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
            detail = await self.investigation_query_service.detail(
                self._identity,
                UUID(args["repository_id"]),
                UUID(args["investigation_id"]),
            )
            result_payload = {
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

        raise ValueError("Unknown or unauthorized tool")
