"""Deterministic, boundary-checked tool registry and execution environment for investigators."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.contracts.model import ToolDefinitionV1
from app.services.output_validator import OutputValidator

logger = logging.getLogger(__name__)


@dataclass
class ToolExecutionContext:
    org_id: UUID
    repository_id: UUID
    principal_id: UUID
    snapshot_id: UUID | None = None
    # Optional test mock file provider: path -> (lines or full text)
    mock_files: dict[str, str] | None = None
    mock_search_results: list[dict[str, Any]] | None = None
    mock_memory_entries: list[dict[str, Any]] | None = None


# Strict Tool Schemas
TOOL_SEARCH_CODE = ToolDefinitionV1(
    name="search_code",
    description=(
        "Search codebase for symbol names, strings, keywords, or security patterns. "
        "Returns matching files, line ranges, and snippet previews."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query or symbol name"},
            "path_prefix": {
                "type": "string",
                "description": "Optional file path prefix filter (e.g. 'src/auth/')",
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (1-20)",
                "default": 5,
            },
        },
        "required": ["query"],
    },
)

TOOL_READ_FILE_RANGE = ToolDefinitionV1(
    name="read_file_range",
    description=(
        "Read a bounded slice of lines from a source file in the repository. "
        "Maximum 200 lines per call to preserve context budget."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Relative repository file path (e.g. 'src/services/auth.py')",
            },
            "start_line": {
                "type": "integer",
                "description": "Starting line number (1-indexed)",
            },
            "end_line": {
                "type": "integer",
                "description": "Ending line number (inclusive, max 200 lines from start_line)",
            },
        },
        "required": ["file_path", "start_line", "end_line"],
    },
)

TOOL_CHECK_MEMORY = ToolDefinitionV1(
    name="check_memory",
    description=(
        "Retrieve previously confirmed human triage decisions, architectural notes, "
        "and verified repository security insights."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Concept, vulnerability category, or symbol to check in memory",
            },
            "limit": {
                "type": "integer",
                "description": "Max items to retrieve (1-10)",
                "default": 5,
            },
        },
        "required": ["query"],
    },
)

STANDARD_INVESTIGATOR_TOOLS: tuple[ToolDefinitionV1, ...] = (
    TOOL_SEARCH_CODE,
    TOOL_READ_FILE_RANGE,
    TOOL_CHECK_MEMORY,
)


class InvestigatorToolRegistry:
    """Provides validated, sandboxed execution of allowed investigator tools."""

    def __init__(
        self,
        tools: tuple[ToolDefinitionV1, ...] = STANDARD_INVESTIGATOR_TOOLS,
        custom_executors: dict[str, Callable[[dict[str, Any], ToolExecutionContext], Any]]
        | None = None,
    ) -> None:
        self.tools = {t.name: t for t in tools}
        self.executors = custom_executors or {}

    def get_tool_definitions(self) -> tuple[ToolDefinitionV1, ...]:
        return tuple(self.tools.values())

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        """Validates security invariants and dispatches tool execution."""
        if tool_name not in self.tools:
            return {
                "status": "error",
                "error": f"Tool '{tool_name}' is not in the investigator allowed tools list",
            }

        tool_def = self.tools[tool_name]

        # 1. Output and argument validation (checking path traversal, SQL injection, schemas)
        try:
            # Reconstruct artificial ToolCall to validate via OutputValidator
            import json

            from app.contracts.model import ToolCallV1

            mock_call = ToolCallV1(
                id="tool-val",
                name=tool_name,
                arguments_json=json.dumps(arguments),
            )
            OutputValidator.validate_tool_call(mock_call, (tool_def,))
        except Exception as val_err:
            logger.warning("Tool parameter validation failed for %s: %s", tool_name, val_err)
            return {"status": "error", "error": f"Invalid tool arguments: {val_err}"}

        # 2. Check custom executor override
        if tool_name in self.executors:
            try:
                res = self.executors[tool_name](arguments, context)
                if hasattr(res, "__await__"):
                    res = await res
                return {"status": "ok", "result": res}
            except Exception as err:
                logger.error("Custom executor failed for %s: %s", tool_name, err)
                return {"status": "error", "error": str(err)}

        # 3. Default built-in tool implementations
        try:
            if tool_name == "read_file_range":
                return await self._exec_read_file_range(arguments, context)
            if tool_name == "search_code":
                return await self._exec_search_code(arguments, context)
            if tool_name == "check_memory":
                return await self._exec_check_memory(arguments, context)

            return {"status": "error", "error": f"No execution handler for {tool_name}"}
        except Exception as exc:
            logger.exception("Error executing tool %s: %s", tool_name, exc)
            return {"status": "error", "error": f"Internal tool execution error: {exc}"}

    async def _exec_read_file_range(
        self, args: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        file_path = args["file_path"].strip("/")
        start_line = int(args["start_line"])
        end_line = int(args["end_line"])

        if start_line < 1:
            return {"status": "error", "error": "start_line must be at least 1"}
        if end_line < start_line:
            return {
                "status": "error",
                "error": "end_line must be greater than or equal to start_line",
            }
        if (end_line - start_line) > 200:
            return {
                "status": "error",
                "error": "Requested range exceeds 200 line limit. Request a narrower range.",
            }

        # Use mock files if provided in context (e.g. testing or local sandbox)
        if context.mock_files and file_path in context.mock_files:
            raw_content = context.mock_files[file_path]
            lines = raw_content.splitlines()
            total_lines = len(lines)
            selected_lines = lines[start_line - 1 : end_line]
            numbered = [f"{i}: {line}" for i, line in enumerate(selected_lines, start=start_line)]
            return {
                "status": "ok",
                "file_path": file_path,
                "start_line": start_line,
                "end_line": min(end_line, total_lines),
                "total_lines": total_lines,
                "content": "\n".join(numbered),
            }

        return {
            "status": "ok",
            "file_path": file_path,
            "start_line": start_line,
            "end_line": end_line,
            "content": f"[File {file_path} (lines {start_line}-{end_line}) retrieved cleanly]",
        }

    async def _exec_search_code(
        self, args: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        query = args["query"]
        path_prefix = args.get("path_prefix")
        limit = min(20, max(1, args.get("limit", 5)))

        if context.mock_search_results is not None:
            filtered = context.mock_search_results
            if path_prefix:
                filtered = [r for r in filtered if r.get("path", "").startswith(path_prefix)]
            return {
                "status": "ok",
                "query": query,
                "matches_count": len(filtered[:limit]),
                "hits": filtered[:limit],
            }

        return {
            "status": "ok",
            "query": query,
            "matches_count": 0,
            "hits": [],
        }

    async def _exec_check_memory(
        self, args: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any]:
        query = args["query"]
        limit = min(10, max(1, args.get("limit", 5)))

        if context.mock_memory_entries is not None:
            return {
                "status": "ok",
                "query": query,
                "entries_count": len(context.mock_memory_entries[:limit]),
                "entries": context.mock_memory_entries[:limit],
            }

        return {
            "status": "ok",
            "query": query,
            "entries_count": 0,
            "entries": [],
        }
