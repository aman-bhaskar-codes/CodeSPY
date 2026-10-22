"""Read-only local Model Context Protocol (MCP) server adhering to JSON-RPC 2.0."""

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
from app.services.context_assembly import ContextAssemblyService
from app.services.hybrid_search import HybridSearchService
from app.services.lexical_search import LexicalSearchService
from app.services.memory_service import MemoryService
from app.services.source_view import SourceViewService


class ReadOnlyMcpServer:
    """Strictly read-only local MCP server providing source view, retrieval, and context tools."""

    SERVER_NAME = "codespy-retrieval-mcp"
    SERVER_VERSION = "1.0.0"

    def __init__(
        self,
        *,
        source_view_service: SourceViewService | None = None,
        lexical_service: LexicalSearchService | None = None,
        hybrid_service: HybridSearchService | None = None,
        context_service: ContextAssemblyService | None = None,
        memory_service: MemoryService | None = None,
    ) -> None:
        self.source_view_service = source_view_service
        self.lexical_service = lexical_service
        self.hybrid_service = hybrid_service
        self.context_service = context_service
        self.memory_service = memory_service

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return MCP tool schemas for read-only retrieval capabilities."""
        return [
            {
                "name": "get_source_view",
                "description": (
                    "Fetch model-safe redacted source code for a verified repository file."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "org_id": {"type": "string", "format": "uuid"},
                        "repository_id": {"type": "string", "format": "uuid"},
                        "snapshot_id": {"type": "string", "format": "uuid"},
                        "principal_id": {"type": "string", "format": "uuid"},
                        "file_id": {"type": "string", "format": "uuid"},
                        "start_byte": {"type": "integer", "minimum": 0},
                        "end_byte": {"type": "integer", "minimum": 0},
                    },
                    "required": [
                        "org_id",
                        "repository_id",
                        "snapshot_id",
                        "principal_id",
                        "file_id",
                    ],
                },
            },
            {
                "name": "search_exact",
                "description": "Execute exact symbol or path search across indexed code chunks.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "org_id": {"type": "string", "format": "uuid"},
                        "repository_id": {"type": "string", "format": "uuid"},
                        "snapshot_id": {"type": "string", "format": "uuid"},
                        "principal_id": {"type": "string", "format": "uuid"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 20},
                    },
                    "required": ["org_id", "repository_id", "snapshot_id", "principal_id", "query"],
                },
            },
            {
                "name": "search_hybrid",
                "description": (
                    "Execute reciprocal rank fused hybrid search (exact + lexical + dense)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "org_id": {"type": "string", "format": "uuid"},
                        "repository_id": {"type": "string", "format": "uuid"},
                        "snapshot_id": {"type": "string", "format": "uuid"},
                        "principal_id": {"type": "string", "format": "uuid"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 20},
                    },
                    "required": ["org_id", "repository_id", "snapshot_id", "principal_id", "query"],
                },
            },
            {
                "name": "assemble_context",
                "description": "Assemble token-budgeted, citation-keyed grounding context packet.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "org_id": {"type": "string", "format": "uuid"},
                        "repository_id": {"type": "string", "format": "uuid"},
                        "snapshot_id": {"type": "string", "format": "uuid"},
                        "principal_id": {"type": "string", "format": "uuid"},
                        "query": {"type": "string"},
                        "max_tokens": {"type": "integer", "default": 4096},
                    },
                    "required": ["org_id", "repository_id", "snapshot_id", "principal_id", "query"],
                },
            },
            {
                "name": "query_memory",
                "description": (
                    "Query evidence-backed repository memories, triage decisions, and findings."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "org_id": {"type": "string", "format": "uuid"},
                        "repository_id": {"type": "string", "format": "uuid"},
                        "principal_id": {"type": "string", "format": "uuid"},
                        "query": {"type": "string"},
                        "topic": {"type": "string"},
                        "limit": {"type": "integer", "default": 20},
                    },
                    "required": ["org_id", "repository_id", "principal_id", "query"],
                },
            },
        ]

    async def handle_request(self, req: dict[str, Any]) -> dict[str, Any]:
        """Process incoming JSON-RPC 2.0 MCP request and route to read-only tool handler."""
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
                content_text = await self._dispatch_tool(tool_name, arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": content_text}],
                        "isError": False,
                    },
                }
            except ScopeViolation as ex:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Security/Scope Violation: {ex}"}],
                        "isError": True,
                    },
                }
            except (ValueError, KeyError) as ex:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Invalid Parameters: {ex}"}],
                        "isError": True,
                    },
                }
            except Exception as ex:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Tool Execution Error: {ex}"}],
                        "isError": True,
                    },
                }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    async def _dispatch_tool(self, name: str, args: dict[str, Any]) -> str:
        """Route tool call to read-only service. Mutating tools are strictly forbidden."""
        if name == "get_source_view":
            if not self.source_view_service:
                raise RuntimeError("SourceViewService not configured on MCP server")
            byte_range = None
            if "start_byte" in args and "end_byte" in args:
                byte_range = ByteRangeV1(start_byte=args["start_byte"], end_byte=args["end_byte"])
            req = SourceViewRequestV1(
                org_id=UUID(args["org_id"]),
                repository_id=UUID(args["repository_id"]),
                snapshot_id=UUID(args["snapshot_id"]),
                principal_id=UUID(args["principal_id"]),
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
                org_id=UUID(args["org_id"]),
                repository_id=UUID(args["repository_id"]),
                snapshot_id=UUID(args["snapshot_id"]),
                principal_id=UUID(args["principal_id"]),
            )
            req_exact = ExactSearchRequestV1(
                scope=scope,
                query=str(args["query"]),
                limit=int(args.get("limit", 20)),
            )
            res_exact = await self.lexical_service.search_exact(req_exact)
            return json.dumps(res_exact.model_dump(mode="json"))

        if name == "search_hybrid":
            if not self.hybrid_service:
                raise RuntimeError("HybridSearchService not configured on MCP server")
            scope = SearchScopeV1(
                org_id=UUID(args["org_id"]),
                repository_id=UUID(args["repository_id"]),
                snapshot_id=UUID(args["snapshot_id"]),
                principal_id=UUID(args["principal_id"]),
            )
            req_hybrid = HybridSearchRequestV1(
                scope=scope,
                query=str(args["query"]),
                limit=int(args.get("limit", 20)),
            )
            res_hybrid = await self.hybrid_service.search_hybrid(req_hybrid)
            return json.dumps(res_hybrid.model_dump(mode="json"))

        if name == "assemble_context":
            if not self.context_service or not self.hybrid_service:
                raise RuntimeError("Context / Hybrid service not configured on MCP server")
            scope = SearchScopeV1(
                org_id=UUID(args["org_id"]),
                repository_id=UUID(args["repository_id"]),
                snapshot_id=UUID(args["snapshot_id"]),
                principal_id=UUID(args["principal_id"]),
            )
            # Run hybrid search first to gather candidates
            h_res = await self.hybrid_service.search_hybrid(
                HybridSearchRequestV1(scope=scope, query=str(args["query"]), limit=20)
            )
            max_tok = int(args.get("max_tokens", 4096))
            c_res = await self.context_service.assemble_context(
                ContextAssemblyRequestV1(
                    scope=scope,
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
                org_id=UUID(args["org_id"]),
                repository_id=UUID(args["repository_id"]),
                principal_id=UUID(args["principal_id"]),
                query=str(args["query"]),
                topic=args.get("topic"),
                limit=int(args.get("limit", 20)),
            )
            m_res = await self.memory_service.query_memory(m_req)
            return json.dumps(m_res.model_dump(mode="json"))

        raise ValueError(f"Unknown or unauthorized tool: {name}")
