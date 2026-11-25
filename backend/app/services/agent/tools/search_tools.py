# ruff: noqa: E501
"""Tools that consult the base snapshot's indexes: structural symbols and ranked retrieval."""

from typing import Annotated

from pydantic import Field

from app.contracts.agent_events import EvidenceRef, SourceRangeProvenance
from app.contracts.agent_tools import AgentToolArgs, AgentToolResultV1
from app.services.agent.tools.base import Tool, ToolContext, fail, succeed
from app.services.agent.tools.git_tools import changes
from app.services.agent.tools.text import evidence_id, is_sensitive

STALE_NOTE = "  [changed in this session: the index describes the base snapshot]"


class FindSymbolArgs(AgentToolArgs):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    kind: Annotated[str, Field(max_length=40)] | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 25


class FindSymbol(Tool):
    name = "find_symbol"
    description = (
        "Find where a function, class or other symbol is defined, using structural analysis of the "
        "snapshot the session started from. Files changed since then are flagged."
    )
    args_model = FindSymbolArgs

    async def run(self, ctx: ToolContext, args: FindSymbolArgs) -> AgentToolResultV1:
        if ctx.symbols is None:
            return fail(
                "unavailable", "no_symbol_index", "Symbol lookup is not available for this session."
            )
        hits = [
            h
            for h in await ctx.symbols.find(args.name, args.kind, args.limit)
            if not is_sensitive(h.path)
        ]
        changed = await changes(ctx) or {}
        lines = [
            f"{h.kind} {h.name}  {h.path}:{h.line}{STALE_NOTE if h.path in changed else ''}"
            for h in hits
        ]
        evidence = [
            EvidenceRef(
                evidence_id=evidence_id("symbol", h.path, h.line, h.name),
                provenance=SourceRangeProvenance(path=h.path, start_line=h.line, end_line=h.line),
            )
            for h in hits[:20]
        ]
        return succeed(
            "\n".join(lines) or "no definitions found",
            summary=f"{len(hits)} definitions of {args.name[:60]!r}",
            partial=len(hits) >= args.limit,
            evidence=evidence,
        )


class SearchSemanticArgs(AgentToolArgs):
    query: Annotated[str, Field(min_length=1, max_length=500)]
    k: Annotated[int, Field(ge=1, le=20)] = 10


class SearchSemantic(Tool):
    name = "search_semantic"
    description = (
        "Search the snapshot the session started from by meaning, not exact text: describe what the "
        "code does. Results in files changed since then are flagged as possibly stale."
    )
    args_model = SearchSemanticArgs

    async def run(self, ctx: ToolContext, args: SearchSemanticArgs) -> AgentToolResultV1:
        if ctx.search is None:
            return fail(
                "unavailable", "no_search_index", "Code search is not available for this session."
            )
        hits = [h for h in await ctx.search.search(args.query, args.k) if not is_sensitive(h.path)]
        changed = await changes(ctx) or {}
        blocks = [
            f"{h.path}:{h.start_line}-{h.end_line}  (score {h.score:.2f}){STALE_NOTE if h.path in changed else ''}\n{h.snippet}"
            for h in hits
        ]
        evidence = [
            EvidenceRef(
                evidence_id=evidence_id("search", h.path, h.start_line, h.end_line),
                provenance=SourceRangeProvenance(
                    path=h.path, start_line=h.start_line, end_line=max(h.end_line, h.start_line)
                ),
            )
            for h in hits[:20]
        ]
        return succeed(
            "\n\n".join(blocks) or "no results",
            summary=f"{len(hits)} results",
            evidence=evidence,
        )
