# ruff: noqa: E501
"""``git_status`` and ``git_diff``: what the session changed against its base snapshot.

The base snapshot is immutable and known by hash, so the comparison needs no git binary in the
sandbox and does not depend on a repository the agent could have altered.
"""

import difflib
from typing import Annotated

from pydantic import Field

from app.contracts.agent_tools import AgentToolArgs, AgentToolResultV1
from app.ports.sandbox import PathRejected, normalize_workspace_path
from app.services.agent.tools.base import Tool, ToolContext, fail, succeed
from app.services.agent.tools.text import is_sensitive

MAX_STATUS_LINES = 500
MAX_DIFF_FILES = 20


async def changes(ctx: ToolContext) -> dict[str, str] | None:
    """Changed paths mapped to ``A`` (added), ``M`` (modified) or ``D`` (deleted); ``None`` without a base."""
    if ctx.base is None:
        return None
    base = await ctx.base.hashes()
    now = await ctx.sandbox.file_hashes(ctx.handle)
    result = {p: "M" for p in now if p in base and now[p] != base[p]}
    result.update({p: "A" for p in now if p not in base})
    result.update({p: "D" for p in base if p not in now})
    return dict(sorted(result.items()))


class NoArgs(AgentToolArgs):
    pass


class GitStatus(Tool):
    name = "git_status"
    description = (
        "List the files this session has added (A), modified (M) or deleted (D) compared with the "
        "snapshot it started from."
    )
    args_model = NoArgs

    async def run(self, ctx: ToolContext, args: NoArgs) -> AgentToolResultV1:
        found = await changes(ctx)
        if found is None:
            return fail("unavailable", "no_base_snapshot", "The base snapshot is not available.")
        shown = list(found.items())[:MAX_STATUS_LINES]
        body = "\n".join(f"{kind} {path}" for path, kind in shown) or "no changes"
        counts = {k: sum(v == k for v in found.values()) for k in "AMD"}
        summary = f"{len(found)} changed files (A {counts['A']}, M {counts['M']}, D {counts['D']})"
        more = f"\n({len(found) - len(shown)} more not shown)" if len(found) > len(shown) else ""
        return succeed(body + more, summary=summary, partial=len(found) > len(shown), data=counts)


class GitDiffArgs(AgentToolArgs):
    paths: (
        Annotated[list[Annotated[str, Field(min_length=1, max_length=1024)]], Field(max_length=20)]
        | None
    ) = None
    context: Annotated[int, Field(ge=0, le=10)] = 3


def _text(data: bytes | None) -> list[str] | None:
    if data is None:
        return []
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return None if "\x00" in decoded else decoded.splitlines(keepends=True)


class GitDiff(Tool):
    name = "git_diff"
    description = (
        "Show a unified diff of this session's changes against the snapshot it started from, for the "
        "given paths or for up to 20 changed files."
    )
    args_model = GitDiffArgs

    async def run(self, ctx: ToolContext, args: GitDiffArgs) -> AgentToolResultV1:
        found = await changes(ctx)
        if found is None or ctx.base is None:
            return fail("unavailable", "no_base_snapshot", "The base snapshot is not available.")
        if args.paths:
            try:
                wanted = [normalize_workspace_path(p) for p in args.paths]
            except PathRejected:
                return fail("denied", "path_rejected", "That path is not allowed in the workspace.")
        else:
            wanted = list(found)
        chosen = [p for p in wanted if p in found][:MAX_DIFF_FILES]
        skipped = len([p for p in wanted if p in found]) - len(chosen)
        parts: list[str] = []
        for path in chosen:
            if is_sensitive(path):
                parts.append(f"{found[path]} {path}: content withheld (credential file)\n")
                continue
            old = _text(await ctx.base.read(path) if found[path] != "A" else None)
            after = None if found[path] == "D" else await ctx.sandbox.read_bytes(ctx.handle, path)
            new = _text(after)
            if old is None or new is None:
                parts.append(f"Binary files a/{path} and b/{path} differ\n")
                continue
            parts.append(
                "".join(
                    difflib.unified_diff(
                        old,
                        new,
                        fromfile="/dev/null" if found[path] == "A" else f"a/{path}",
                        tofile="/dev/null" if found[path] == "D" else f"b/{path}",
                        n=args.context,
                    )
                )
                or f"{path}: only permissions or line endings differ\n"
            )
        body = "\n".join(p if p.endswith("\n") else p + "\n" for p in parts) or "no changes"
        if skipped > 0:
            body += f"\n({skipped} more changed files not shown; pass paths to see them)"
        return succeed(
            body,
            summary=f"diff of {len(chosen)} files",
            partial=skipped > 0,
            data={"files": len(chosen)},
        )
