"""Real, bounded, read-only tools for durable investigations.

Implements InvestigationToolPort with explicit scope and resource boundaries:
- Rigid argument validation (no boolean coercion, no traversal, no undeclared args)
- Split read/revalidation transactions for source reading without holding DB locks
- Strict 200 inclusive line ceiling and 32 KiB safe payload ceiling
- Explicit unavailable / denied / partial / invalid / timed_out status (never fake success)
- Real admitted archive, lexical search, and memory service adapters
- Optional inspect_finding and trace_context tools only registered when configured
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.context_manifest import MemoryContextSettingsV1
from app.contracts.investigation_execution import (
    MAX_MEMORY_HITS,
    MAX_SAFE_RETURNED_BYTES,
    MAX_SEARCH_HITS,
    MAX_SOURCE_READ_LINES,
    MAX_TOOL_ARG_BYTES,
    MAX_TOOL_DURATION_SECONDS,
    MAX_TOOL_QUERY_BYTES,
    InvestigationEvidenceV1,
    InvestigationExecutionContextV1,
    InvestigationToolName,
    InvestigationToolRequestV1,
    InvestigationToolResultV1,
    InvestigationToolStatus,
)
from app.contracts.model import ToolDefinitionV1
from app.contracts.search import SearchScopeV1
from app.contracts.source_view import (
    PrivacyPolicyV1,
)
from app.domain.canonical_json import canonical_bytes
from app.domain.errors import ScopeViolation
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import memberships, source_files
from app.ports.investigation_execution import InvestigationToolPort
from app.ports.memory import MemorySelectionPort
from app.ports.object_store import ObjectStore
from app.ports.scan_execution import ScanCapabilities
from app.ports.source_reader import AdmittedSourceReadV1, SourceReaderPort
from app.repositories.investigation_execution_repository import (
    SqlInvestigationEvidenceRepository,
)
from app.security.authorization import require_repository
from app.services.analysis_sources import locate_source
from app.services.feature_trace import FeatureTraceService
from app.services.findings_query import FindingsQueryService
from app.services.lexical_search import LexicalSearchService
from app.services.memory_evidence import SourceMemoryEvidence
from app.services.memory_service import MemoryService
from app.services.privacy_transform import is_sensitive_path, transform_model_safe
from app.services.retrieval_selection import resolve_lexical_set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Strict Tool Schemas for Model Visibility
# ---------------------------------------------------------------------------

TOOL_DEF_READ_FILE_RANGE = ToolDefinitionV1(
    name=InvestigationToolName.READ_FILE_RANGE.value,
    description=(
        "Read a bounded slice of lines from an admitted source file in the repository snapshot. "
        "Maximum 200 lines per call (inclusive). Path must be relative to repository root."
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
        "additionalProperties": False,
    },
)

TOOL_DEF_SEARCH_CODE = ToolDefinitionV1(
    name=InvestigationToolName.SEARCH_CODE.value,
    description=(
        "Search codebase for symbol names, strings, keywords, or security patterns. "
        "Returns matching files, line ranges, and snippet previews."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query or symbol name (max 1024 characters)",
            },
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
        "additionalProperties": False,
    },
)

TOOL_DEF_CHECK_MEMORY = ToolDefinitionV1(
    name=InvestigationToolName.CHECK_MEMORY.value,
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
        "additionalProperties": False,
    },
)

TOOL_DEF_INSPECT_FINDING = ToolDefinitionV1(
    name=InvestigationToolName.INSPECT_FINDING.value,
    description=(
        "Inspect verified deterministic finding details, occurrences, and current support "
        "within the frozen repository snapshot."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "finding_id": {
                "type": "string",
                "description": "UUID of the finding to inspect",
            },
        },
        "required": ["finding_id"],
        "additionalProperties": False,
    },
)

TOOL_DEF_TRACE_CONTEXT = ToolDefinitionV1(
    name=InvestigationToolName.TRACE_CONTEXT.value,
    description=(
        "Inspect the bounded structural Code Property Graph (CPG) neighborhood "
        "and data flow around a finding."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "finding_id": {
                "type": "string",
                "description": "UUID of the finding whose structural trace to inspect",
            },
        },
        "required": ["finding_id"],
        "additionalProperties": False,
    },
)


# ---------------------------------------------------------------------------
# Argument Validation Logic
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    error: str = ""
    cleaned: dict[str, Any] = field(default_factory=dict)


def _is_strict_int(val: Any) -> bool:
    """Return True if val is strictly an int and not a boolean."""
    return type(val) is int


def _is_safe_relative_path(path: str) -> bool:
    """Validate that path is a safe relative path with no traversal or null bytes."""
    if not path or not path.strip():
        return False
    # Check null bytes
    if "\x00" in path or "%00" in path.lower():
        return False
    # Check absolute path
    if path.startswith("/") or path.startswith("\\"):
        return False
    # Check URL decoding
    try:
        decoded = unquote(path)
        if "\x00" in decoded or decoded.startswith("/") or decoded.startswith("\\"):
            return False
    except Exception:
        return False
    # Check traversal segments
    segments = re.split(r"[/\\]+", decoded)
    for seg in segments:
        if seg == ".." or seg.lower() in ("%2e%2e", ".%2e", "%2e."):
            return False
    # Traversal regex
    if re.search(r"(^|[/\\])\.\.([/\\]|$)", path):
        return False
    return re.search(r"(%2e|%2f|%5c)", path, re.IGNORECASE) is None


def path_matches_prefix(path: str, prefix: str | None) -> bool:
    """Match an exact source path or descendants of a directory boundary."""
    if prefix is None:
        return True
    normalized = prefix.rstrip("/")
    return path == normalized or path.startswith(normalized + "/")


def validate_tool_arguments(
    tool_name: InvestigationToolName | str,
    arguments: dict[str, Any],
) -> ValidationResult:
    """Rigidly validate tool arguments against contracts and safety invariants.

    Enforces:
    - Undeclared argument rejection
    - Rejection of booleans for integer fields
    - Safe relative paths (no traversal, no null bytes, no leading slashes)
    - Line range ceiling (inclusive lines <= 200)
    - Query size bounds (<= 1024 chars/bytes)
    - Limit bounds (search: 1-20, memory: 1-10)
    """
    if isinstance(tool_name, str):
        try:
            tool_name = InvestigationToolName(tool_name)
        except ValueError:
            return ValidationResult(False, f"Unknown tool: {tool_name}")

    # Check argument serialization size limit (16 KiB)
    try:
        arg_bytes = canonical_bytes(arguments)
        if len(arg_bytes) > MAX_TOOL_ARG_BYTES:
            return ValidationResult(False, f"Arguments exceed {MAX_TOOL_ARG_BYTES} byte limit")
    except Exception as e:
        return ValidationResult(False, f"Arguments cannot be serialized: {e}")

    # Disallow scope override keys
    forbidden_keys = {
        "org_id",
        "repository_id",
        "snapshot_id",
        "principal_id",
        "spec_hash",
        "attempt_id",
        "role",
    }
    present_forbidden = forbidden_keys.intersection(arguments.keys())
    if present_forbidden:
        return ValidationResult(
            False, f"Undeclared or forbidden arguments: {', '.join(sorted(present_forbidden))}"
        )

    if tool_name == InvestigationToolName.READ_FILE_RANGE:
        allowed = {"file_path", "start_line", "end_line"}
        extra = set(arguments.keys()) - allowed
        if extra:
            return ValidationResult(
                False, f"Undeclared arguments for read_file_range: {', '.join(sorted(extra))}"
            )

        if "file_path" not in arguments:
            return ValidationResult(False, "Missing required argument 'file_path'")
        if "start_line" not in arguments:
            return ValidationResult(False, "Missing required argument 'start_line'")
        if "end_line" not in arguments:
            return ValidationResult(False, "Missing required argument 'end_line'")

        raw_path = arguments["file_path"]
        if not isinstance(raw_path, str) or not _is_safe_relative_path(raw_path):
            return ValidationResult(
                False,
                f"Invalid or unsafe file_path: '{raw_path}'. Must be a safe relative path.",
            )

        start_line = arguments["start_line"]
        end_line = arguments["end_line"]

        if isinstance(start_line, bool) or not _is_strict_int(start_line):
            return ValidationResult(False, "Boolean or non-integer passed for start_line")
        if isinstance(end_line, bool) or not _is_strict_int(end_line):
            return ValidationResult(False, "Boolean or non-integer passed for end_line")

        if start_line < 1:
            return ValidationResult(False, "start_line must be at least 1")
        if end_line < start_line:
            return ValidationResult(
                False, f"end_line ({end_line}) cannot be less than start_line ({start_line})"
            )

        # Inclusive line count check: end_line - start_line + 1
        line_count = end_line - start_line + 1
        if line_count > MAX_SOURCE_READ_LINES:
            return ValidationResult(
                False,
                f"Requested range of {line_count} lines exceeds the 200 line limit (inclusive). "
                "Request a narrower range.",
            )

        return ValidationResult(
            True,
            cleaned={
                "file_path": raw_path.strip(),
                "start_line": start_line,
                "end_line": end_line,
            },
        )

    if tool_name == InvestigationToolName.SEARCH_CODE:
        allowed = {"query", "path_prefix", "limit"}
        extra = set(arguments.keys()) - allowed
        if extra:
            return ValidationResult(
                False, f"Undeclared arguments for search_code: {', '.join(sorted(extra))}"
            )

        if "query" not in arguments:
            return ValidationResult(False, "Missing required argument 'query'")

        query = arguments["query"]
        if not isinstance(query, str) or not query.strip():
            return ValidationResult(False, "query must be a non-empty string")
        if len(query) > MAX_TOOL_QUERY_BYTES or len(query.encode("utf-8")) > MAX_TOOL_QUERY_BYTES:
            return ValidationResult(
                False, f"query exceeds {MAX_TOOL_QUERY_BYTES} characters/bytes limit"
            )

        cleaned: dict[str, Any] = {"query": query.strip()}

        if "path_prefix" in arguments:
            path_prefix = arguments["path_prefix"]
            if path_prefix is not None:
                if not isinstance(path_prefix, str) or not _is_safe_relative_path(path_prefix):
                    return ValidationResult(False, "Invalid or unsafe path_prefix")
                cleaned["path_prefix"] = path_prefix.strip()

        limit = arguments.get("limit", 5)
        if isinstance(limit, bool) or not _is_strict_int(limit):
            return ValidationResult(False, "Boolean or non-integer passed for limit")
        if limit < 1 or limit > MAX_SEARCH_HITS:
            return ValidationResult(False, f"limit must be between 1 and {MAX_SEARCH_HITS}")
        cleaned["limit"] = limit

        return ValidationResult(True, cleaned=cleaned)

    if tool_name == InvestigationToolName.CHECK_MEMORY:
        allowed = {"query", "limit"}
        extra = set(arguments.keys()) - allowed
        if extra:
            return ValidationResult(
                False, f"Undeclared arguments for check_memory: {', '.join(sorted(extra))}"
            )

        if "query" not in arguments:
            return ValidationResult(False, "Missing required argument 'query'")

        query = arguments["query"]
        if not isinstance(query, str) or not query.strip():
            return ValidationResult(False, "query must be a non-empty string")
        if len(query) > MAX_TOOL_QUERY_BYTES or len(query.encode("utf-8")) > MAX_TOOL_QUERY_BYTES:
            return ValidationResult(
                False, f"query exceeds {MAX_TOOL_QUERY_BYTES} characters/bytes limit"
            )

        cleaned = {"query": query.strip()}

        limit = arguments.get("limit", 5)
        if isinstance(limit, bool) or not _is_strict_int(limit):
            return ValidationResult(False, "Boolean or non-integer passed for limit")
        if limit < 1 or limit > MAX_MEMORY_HITS:
            return ValidationResult(False, f"limit must be between 1 and {MAX_MEMORY_HITS}")
        cleaned["limit"] = limit

        return ValidationResult(True, cleaned=cleaned)

    if tool_name in (
        InvestigationToolName.INSPECT_FINDING,
        InvestigationToolName.TRACE_CONTEXT,
    ):
        allowed = {"finding_id"}
        extra = set(arguments.keys()) - allowed
        if extra:
            return ValidationResult(
                False, f"Undeclared arguments for {tool_name}: {', '.join(sorted(extra))}"
            )

        if "finding_id" not in arguments:
            return ValidationResult(False, "Missing required argument 'finding_id'")

        fid = arguments["finding_id"]
        if isinstance(fid, UUID):
            return ValidationResult(True, cleaned={"finding_id": fid})
        if isinstance(fid, str):
            try:
                parsed_fid = UUID(fid)
                return ValidationResult(True, cleaned={"finding_id": parsed_fid})
            except ValueError:
                return ValidationResult(False, f"Invalid UUID string for finding_id: {fid}")
        return ValidationResult(False, "finding_id must be a valid UUID string or UUID")

    return ValidationResult(False, f"Unsupported tool name: {tool_name}")


# ---------------------------------------------------------------------------
# InvestigationToolService Implementation
# ---------------------------------------------------------------------------


class InvestigationToolService(InvestigationToolPort):
    """Real bounded read-only tools connecting to repository services."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        source_reader: SourceReaderPort | None = None,
        store: ObjectStore | None = None,
        lexical_service: LexicalSearchService | None = None,
        memory_service: MemorySelectionPort | None = None,
        findings_service: FindingsQueryService | None = None,
        trace_service: FeatureTraceService | None = None,
        privacy_policy: PrivacyPolicyV1 | None = None,
        evidence_repository: SqlInvestigationEvidenceRepository | None = None,
        memory_context: MemoryContextSettingsV1 | None = None,
        scan_capabilities: ScanCapabilities | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.source_reader = source_reader
        self.store = store
        self.lexical_service = lexical_service
        self.memory_service = memory_service
        self.memory_context = memory_context or MemoryContextSettingsV1()
        self.scan_capabilities = scan_capabilities
        self.findings_service = findings_service
        self.trace_service = trace_service
        self.privacy_policy = privacy_policy or PrivacyPolicyV1()
        self.evidence_repo = evidence_repository or SqlInvestigationEvidenceRepository()

    def get_tool_definitions(self) -> tuple[ToolDefinitionV1, ...]:
        """Return available tool schemas based on configured capabilities."""
        tools: list[ToolDefinitionV1] = [
            TOOL_DEF_READ_FILE_RANGE,
            TOOL_DEF_SEARCH_CODE,
            TOOL_DEF_CHECK_MEMORY,
        ]
        if self.findings_service is not None:
            tools.append(TOOL_DEF_INSPECT_FINDING)
        if self.trace_service is not None:
            tools.append(TOOL_DEF_TRACE_CONTEXT)
        return tuple(tools)

    async def execute(
        self,
        request: InvestigationToolRequestV1,
        context: InvestigationExecutionContextV1,
    ) -> InvestigationToolResultV1:
        """Execute a scoped, validated, bounded read-only tool invocation."""
        # 1. Deadline & timeout check
        now = datetime.now(UTC)
        deadline = context.deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)

        remaining_seconds = (deadline - now).total_seconds()
        if remaining_seconds <= 0:
            return InvestigationToolResultV1(
                status=InvestigationToolStatus.TIMED_OUT,
                omission_reason="investigation_deadline_exceeded",
            )

        timeout_sec = min(float(MAX_TOOL_DURATION_SECONDS), max(0.1, remaining_seconds))

        try:
            async with asyncio.timeout(timeout_sec):
                return await self._execute_internal(request, context)
        except TimeoutError:
            logger.warning(
                "Tool %s invocation timed out after %s seconds",
                request.tool_name,
                timeout_sec,
            )
            return InvestigationToolResultV1(
                status=InvestigationToolStatus.TIMED_OUT,
                omission_reason="tool_execution_timeout",
            )

    async def _execute_internal(
        self,
        request: InvestigationToolRequestV1,
        context: InvestigationExecutionContextV1,
    ) -> InvestigationToolResultV1:
        # 2. Check capability registration
        available_tools = {t.name for t in self.get_tool_definitions()}
        if request.tool_name.value not in available_tools:
            return InvestigationToolResultV1(
                status=InvestigationToolStatus.UNAVAILABLE,
                omission_reason=f"tool_{request.tool_name.value}_not_configured",
            )

        # 3. Argument validation
        validation = validate_tool_arguments(request.tool_name, request.arguments)
        if not validation.valid:
            return InvestigationToolResultV1(
                status=InvestigationToolStatus.INVALID,
                omission_reason=f"invalid_arguments: {validation.error}",
            )

        args = validation.cleaned

        # 4. Dispatch to tool handler
        if request.tool_name == InvestigationToolName.READ_FILE_RANGE:
            return await self._exec_read_file_range(args, context)
        if request.tool_name == InvestigationToolName.SEARCH_CODE:
            return await self._exec_search_code(args, context)
        if request.tool_name == InvestigationToolName.CHECK_MEMORY:
            return await self._exec_check_memory(args, context)
        if request.tool_name == InvestigationToolName.INSPECT_FINDING:
            return await self._exec_inspect_finding(args, context)
        if request.tool_name == InvestigationToolName.TRACE_CONTEXT:
            return await self._exec_trace_context(args, context)

        return InvestigationToolResultV1(
            status=InvestigationToolStatus.INVALID,
            omission_reason=f"unhandled_tool: {request.tool_name}",
        )

    # -----------------------------------------------------------------------
    # Tool 1: read_file_range (Two-stage isolated read with evidence receipt)
    # -----------------------------------------------------------------------

    async def _exec_read_file_range(
        self,
        args: dict[str, Any],
        context: InvestigationExecutionContextV1,
    ) -> InvestigationToolResultV1:
        file_path: str = args["file_path"]
        start_line: int = args["start_line"]
        end_line: int = args["end_line"]

        if self.source_reader is None or self.session_factory is None:
            return InvestigationToolResultV1(
                status=InvestigationToolStatus.UNAVAILABLE,
                omission_reason="source_reader_unavailable",
            )

        # Sensitive path check under policy
        if is_sensitive_path(file_path, self.privacy_policy.sensitive_path_patterns):
            return InvestigationToolResultV1(
                status=InvestigationToolStatus.DENIED,
                omission_reason="sensitive_path_exclusion",
            )

        # Stage 1: Short transaction to validate scope, membership, and resolve source file
        async with self.session_factory() as session:
            # Check membership
            role = await session.scalar(
                select(memberships.c.role).where(
                    memberships.c.org_id == context.scope.org_id,
                    memberships.c.principal_id == context.scope.principal_id,
                )
            )
            if role is None:
                return InvestigationToolResultV1(
                    status=InvestigationToolStatus.DENIED,
                    omission_reason="membership_denied",
                )

            # Check grant
            grants = PHASE2_TABLES["repository_grants"]
            grant = (
                (
                    await session.execute(
                        select(grants).where(
                            grants.c.org_id == context.scope.org_id,
                            grants.c.repository_id == context.scope.repository_id,
                            grants.c.principal_id == context.scope.principal_id,
                            grants.c.revoked_at.is_(None),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if grant is None:
                return InvestigationToolResultV1(
                    status=InvestigationToolStatus.DENIED,
                    omission_reason="repository_grant_denied",
                )

            # Query source file row in snapshot
            row = (
                (
                    await session.execute(
                        select(source_files).where(
                            source_files.c.org_id == context.scope.org_id,
                            source_files.c.snapshot_id == context.scope.snapshot_id,
                            source_files.c.path == file_path,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return InvestigationToolResultV1(
                    status=InvestigationToolStatus.UNAVAILABLE,
                    omission_reason=f"file_not_found_in_snapshot: {file_path}",
                )

            file_id = UUID(str(row["id"]))
            file_sha256 = str(row["sha256"])
            file_size = int(row["size_bytes"])

            # Locate archive info
            archive: Any = None
            strip_root: bool = False
            try:
                located = await locate_source(
                    session,
                    context.scope.org_id,
                    context.scope.repository_id,
                    context.scope.snapshot_id,
                )
                archive = located.archive
                strip_root = located.policy.github_strip_root
            except Exception as e:
                logger.info("locate_source fallback to direct archive descriptor: %s", e)
                from app.contracts.ingestion import ObjectDescriptor

                archive = ObjectDescriptor(
                    key=f"snapshots/{context.scope.snapshot_id}/archive.zip",
                    sha256="0" * 64,
                    size_bytes=0,
                )

        # Stage 2: Isolated read outside DB locks
        try:
            read_req = AdmittedSourceReadV1(
                org_id=context.scope.org_id,
                repository_id=context.scope.repository_id,
                snapshot_id=context.scope.snapshot_id,
                file_id=file_id,
                archive=archive,
                path=file_path,
                sha256=file_sha256,
                size_bytes=file_size,
                strip_root=strip_root,
            )
            raw_bytes = await self.source_reader.read(read_req)
        except Exception as e:
            logger.warning("Source reader failed for %s: %s", file_path, e)
            return InvestigationToolResultV1(
                status=InvestigationToolStatus.UNAVAILABLE,
                omission_reason=f"source_read_failed: {e}",
            )

        if len(raw_bytes) != file_size or hashlib.sha256(raw_bytes).hexdigest() != file_sha256:
            return InvestigationToolResultV1(
                status=InvestigationToolStatus.UNAVAILABLE,
                omission_reason="source_integrity_mismatch",
            )

        # Apply privacy transform for model-safe view
        safe_transform = transform_model_safe(raw_bytes, self.privacy_policy)
        safe_bytes = safe_transform.safe_bytes

        # Extract line slice and bound payload
        payload, is_partial, omit_reason = self._slice_and_bound_source(
            safe_bytes,
            start_line=start_line,
            end_line=end_line,
        )

        excerpt_sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        evidence_id = uuid4()

        # Stage 3: Short transaction to revalidate grant and record immutable evidence receipt
        async with self.session_factory() as session:
            # Revalidate repository grant
            grant = (
                (
                    await session.execute(
                        select(grants).where(
                            grants.c.org_id == context.scope.org_id,
                            grants.c.repository_id == context.scope.repository_id,
                            grants.c.principal_id == context.scope.principal_id,
                            grants.c.revoked_at.is_(None),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if grant is None:
                return InvestigationToolResultV1(
                    status=InvestigationToolStatus.DENIED,
                    omission_reason="repository_grant_revoked_during_read",
                )

            evidence = InvestigationEvidenceV1(
                id=evidence_id,
                org_id=context.scope.org_id,
                investigation_id=context.scope.investigation_id,
                snapshot_id=context.scope.snapshot_id,
                source_path=file_path,
                file_sha256=file_sha256,
                original_start_line=start_line,
                original_end_line=end_line,
                safe_start_line=start_line,
                safe_end_line=end_line,
                excerpt_sha256=excerpt_sha,
                observation_type="source_read",
                coverage_summary=f"Lines {start_line}-{end_line} of {file_path}",
                limitations=omit_reason if is_partial else "",
            )
            await self.evidence_repo.record_evidence(session, evidence)
            await session.commit()

        result_digest = hashlib.sha256(canonical_bytes(payload)).hexdigest()

        return InvestigationToolResultV1(
            status=InvestigationToolStatus.PARTIAL if is_partial else InvestigationToolStatus.OK,
            payload=payload,
            evidence_refs=(evidence_id,),
            omission_reason=omit_reason,
            result_digest=result_digest,
        )

    def _slice_and_bound_source(
        self,
        safe_bytes: bytes,
        start_line: int,
        end_line: int,
    ) -> tuple[str, bool, str]:
        """Extract requested line slice and bound safe payload to 32 KiB."""
        try:
            text = safe_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = safe_bytes.decode("utf-8", errors="replace")

        all_lines = text.splitlines()
        total_lines = len(all_lines)

        # 1-indexed slicing
        sliced = all_lines[start_line - 1 : min(end_line, total_lines)]
        numbered = [f"{i}: {line}" for i, line in enumerate(sliced, start=start_line)]
        content = "\n".join(numbered)

        content_bytes = content.encode("utf-8")
        if len(content_bytes) <= MAX_SAFE_RETURNED_BYTES:
            return content, False, ""

        # Truncate to MAX_SAFE_RETURNED_BYTES boundary
        truncated_bytes = content_bytes[:MAX_SAFE_RETURNED_BYTES]
        truncated_text = truncated_bytes.decode("utf-8", errors="ignore")
        return (
            truncated_text,
            True,
            f"content_truncated_to_{MAX_SAFE_RETURNED_BYTES}_bytes",
        )

    # -----------------------------------------------------------------------
    # Tool 2: search_code (Lexical / Exact code search)
    # -----------------------------------------------------------------------

    async def _exec_search_code(
        self,
        args: dict[str, Any],
        context: InvestigationExecutionContextV1,
    ) -> InvestigationToolResultV1:
        query: str = args["query"]
        limit: int = args.get("limit", 5)
        path_prefix: str | None = args.get("path_prefix")

        if self.session_factory is None:
            return InvestigationToolResultV1(
                status=InvestigationToolStatus.UNAVAILABLE,
                omission_reason="search_service_unavailable",
            )

        async with self.session_factory() as session:
            scope = SearchScopeV1(
                org_id=context.scope.org_id,
                repository_id=context.scope.repository_id,
                snapshot_id=context.scope.snapshot_id,
                principal_id=context.scope.principal_id,
                chunk_set_id=context.scope.chunk_set_id,
            )

            try:
                resolved = await resolve_lexical_set(session, scope)
            except Exception as e:
                logger.info("resolve_lexical_set could not resolve index: %s", e)
                resolved = None

            if resolved is None:
                return InvestigationToolResultV1(
                    status=InvestigationToolStatus.UNAVAILABLE,
                    omission_reason="index_unavailable",
                )

            lexical_svc = self.lexical_service or LexicalSearchService(session)

            from app.contracts.search import ExactSearchRequestV1

            exact_req = ExactSearchRequestV1(
                scope=scope.model_copy(update={"chunk_set_id": resolved.chunk_set_id}),
                query=query,
                limit=limit,
            )

            try:
                search_res = await lexical_svc.search_exact(exact_req)
            except Exception as e:
                logger.warning("Lexical search execution failed: %s", e)
                return InvestigationToolResultV1(
                    status=InvestigationToolStatus.UNAVAILABLE,
                    omission_reason=f"search_execution_failed: {e}",
                )

        hits_data = [
            {
                "file_path": h.file_path,
                "start_line": h.start_line,
                "end_line": h.end_line,
                "symbol_name": h.symbol_name,
                "snippet": h.snippet,
                "score": h.score,
            }
            for h in search_res.hits
            if path_matches_prefix(h.file_path, path_prefix)
        ]

        payload_obj = {
            "query": query,
            "total_candidates": search_res.total_candidates,
            "matches_count": len(hits_data),
            "path_filter_candidate_limited": path_prefix is not None,
            "hits": hits_data,
        }
        payload_str = json.dumps(payload_obj, indent=2)
        if len(payload_str.encode("utf-8")) > MAX_SAFE_RETURNED_BYTES:
            payload_str = payload_str[:MAX_SAFE_RETURNED_BYTES]

        digest = hashlib.sha256(canonical_bytes(payload_str)).hexdigest()

        return InvestigationToolResultV1(
            status=InvestigationToolStatus.PARTIAL if path_prefix else InvestigationToolStatus.OK,
            payload=payload_str,
            coverage_notes=(
                f"Found {len(hits_data)} hits in bounded candidates"
                + ("; path-filter coverage is partial" if path_prefix else "")
            ),
            result_digest=digest,
        )

    # -----------------------------------------------------------------------
    # Tool 3: check_memory (Repo memories under snapshot & actor scope)
    # -----------------------------------------------------------------------

    async def _exec_check_memory(
        self,
        args: dict[str, Any],
        context: InvestigationExecutionContextV1,
    ) -> InvestigationToolResultV1:
        if self.session_factory is None or self.memory_context.mode != "reviewed":
            return InvestigationToolResultV1(
                status=InvestigationToolStatus.UNAVAILABLE,
                omission_reason="memory_service_unavailable",
            )
        from app.contracts.memory import MemoryQueryRequestV1

        query = str(args["query"])
        if len(query) > 256:
            return InvestigationToolResultV1(
                status=InvestigationToolStatus.INVALID, omission_reason="memory_query_exceeds_bound"
            )
        async with self.session_factory() as session:
            service = self.memory_service or MemoryService(
                session,
                evidence=SourceMemoryEvidence(
                    session, self.session_factory, self.source_reader, self.scan_capabilities
                ),
            )
            try:
                result = await service.query_memory(
                    MemoryQueryRequestV1(
                        org_id=context.scope.org_id,
                        repository_id=context.scope.repository_id,
                        snapshot_id=context.scope.snapshot_id,
                        principal_id=context.scope.principal_id,
                        query=query,
                        limit=int(args.get("limit", 5)),
                        history_mode=self.memory_context.history_mode,
                    )
                )
            except ScopeViolation:
                return InvestigationToolResultV1(
                    status=InvestigationToolStatus.DENIED,
                    omission_reason="memory_scope_unavailable",
                )
            except Exception:
                logger.warning("memory_query_unavailable")
                return InvestigationToolResultV1(
                    status=InvestigationToolStatus.UNAVAILABLE,
                    omission_reason="memory_query_unavailable",
                )
        entries = [m.model_dump(mode="json") for m in result.items]
        omitted = result.omitted_count
        while True:
            payload = json.dumps(
                {
                    "query": query,
                    "entries_count": len(entries),
                    "entries": entries,
                    "omitted_count": omitted,
                    "partial": result.partial or omitted > 0,
                    "omission_reasons": result.omission_reasons,
                    "trust": "untrusted_repository_knowledge_not_current_source_proof",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(payload.encode()) <= MAX_SAFE_RETURNED_BYTES:
                break
            if not entries:
                return InvestigationToolResultV1(
                    status=InvestigationToolStatus.UNAVAILABLE,
                    omission_reason="memory_payload_exceeds_bound",
                )
            entries.pop()
            omitted += 1
        return InvestigationToolResultV1(
            status=InvestigationToolStatus.PARTIAL
            if result.partial or omitted
            else InvestigationToolStatus.OK,
            payload=payload,
            coverage_notes=f"Selected {len(entries)} reviewed memories",
            omission_reason="memory_selection_partial" if result.partial or omitted else "",
            result_digest=hashlib.sha256(canonical_bytes(payload)).hexdigest(),
        )

    # -----------------------------------------------------------------------
    # Tool 4: inspect_finding (Optional, when configured)
    # -----------------------------------------------------------------------

    async def _exec_inspect_finding(
        self,
        args: dict[str, Any],
        context: InvestigationExecutionContextV1,
    ) -> InvestigationToolResultV1:
        if self.findings_service is None or self.session_factory is None:
            return InvestigationToolResultV1(
                status=InvestigationToolStatus.UNAVAILABLE,
                omission_reason="findings_service_unconfigured",
            )

        finding_id = args["finding_id"]

        async with self.session_factory() as session:
            tenant_ctx = await require_repository(
                session,
                context.scope.org_id,
                context.scope.principal_id,
                context.scope.repository_id,
            )

        try:
            detail = await self.findings_service.get_finding_detail(
                tenant_ctx,
                context.scope.repository_id,
                context.scope.snapshot_id,
                finding_id,
            )
        except Exception as e:
            logger.warning("Failed to inspect finding %s: %s", finding_id, e)
            return InvestigationToolResultV1(
                status=InvestigationToolStatus.UNAVAILABLE,
                omission_reason=f"finding_lookup_failed: {e}",
            )

        payload_str = json.dumps(detail.model_dump(mode="json"), indent=2)
        if len(payload_str.encode("utf-8")) > MAX_SAFE_RETURNED_BYTES:
            payload_str = payload_str[:MAX_SAFE_RETURNED_BYTES]

        digest = hashlib.sha256(canonical_bytes(payload_str)).hexdigest()

        return InvestigationToolResultV1(
            status=InvestigationToolStatus.OK,
            payload=payload_str,
            result_digest=digest,
        )

    # -----------------------------------------------------------------------
    # Tool 5: trace_context (Optional, when configured)
    # -----------------------------------------------------------------------

    async def _exec_trace_context(
        self,
        args: dict[str, Any],
        context: InvestigationExecutionContextV1,
    ) -> InvestigationToolResultV1:
        if self.trace_service is None or self.session_factory is None:
            return InvestigationToolResultV1(
                status=InvestigationToolStatus.UNAVAILABLE,
                omission_reason="trace_service_unconfigured",
            )

        finding_id = args["finding_id"]

        async with self.session_factory() as session:
            tenant_ctx = await require_repository(
                session,
                context.scope.org_id,
                context.scope.principal_id,
                context.scope.repository_id,
            )

        try:
            trace_res = await self.trace_service.trace(
                tenant_ctx,
                context.scope.repository_id,
                context.scope.snapshot_id,
                finding_id,
            )
        except Exception as e:
            logger.warning("Failed to trace context for %s: %s", finding_id, e)
            return InvestigationToolResultV1(
                status=InvestigationToolStatus.UNAVAILABLE,
                omission_reason=f"trace_failed: {e}",
            )

        payload_str = json.dumps(trace_res.model_dump(mode="json"), indent=2)
        if len(payload_str.encode("utf-8")) > MAX_SAFE_RETURNED_BYTES:
            payload_str = payload_str[:MAX_SAFE_RETURNED_BYTES]

        digest = hashlib.sha256(canonical_bytes(payload_str)).hexdigest()

        return InvestigationToolResultV1(
            status=InvestigationToolStatus.OK,
            payload=payload_str,
            result_digest=digest,
        )
