# ruff: noqa: E501
"""Edit tools: ``apply_patch``, ``create_file``, ``delete_path`` and ``move_path``.

Each edit is planned before it is applied. Planning reads the files, checks every expected hash and
every SEARCH block, and produces the change set with its diff; nothing is written. The runner shows
that plan for approval and then applies it, so what is approved is exactly what is written. A plan
applies atomically: if any part fails, no file changes. Applying twice is a no-op.
"""

import base64
import difflib
import json
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import Field

from app.contracts.agent_events import Hunk
from app.contracts.agent_tools import AgentToolArgs, AgentToolResultV1
from app.contracts.workspace import FileChangeV1
from app.domain.canonical_json import semantic_digest
from app.ports.sandbox import (
    MAX_FILE_BYTES,
    PathRejected,
    WorkspaceConflict,
    WorkspaceFileNotFound,
    content_hash,
    normalize_workspace_path,
)
from app.services.agent.tools.base import Tool, ToolContext, fail, succeed
from app.services.agent.tools.text import is_sensitive, redact, truncate_head_tail
from app.services.workspace.changes import change_digest

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
MAX_PATCH_BYTES = 1024 * 1024
MAX_EXCERPT_BYTES = 4 * 1024
MAX_PREVIEW_BYTES = 6 * 1024
MAX_FAILURES_SHOWN = 5


@dataclass(frozen=True)
class PlannedFile:
    path: str
    change: str  # modify, create, delete or rename, as in the event protocol
    before_hash: str | None
    after_hash: str | None
    hunks: tuple[Hunk, ...]
    renamed_from: str | None = None
    added: int = 0
    removed: int = 0


@dataclass(frozen=True)
class EditPlan:
    changes: tuple[FileChangeV1, ...]
    files: tuple[PlannedFile, ...]
    digest: str
    summary: str
    #: A unified diff of the whole plan, redacted and bounded, for the approval card and the model.
    preview: str
    #: Secrets replaced by markers in ``preview``.
    redactions: int = 0


def _lines(data: bytes) -> list[str]:
    return data.decode("utf-8").splitlines(keepends=True)


def _hunks(old: list[str], new: list[str]) -> tuple[Hunk, ...]:
    out: list[Hunk] = []
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for index, group in enumerate(list(matcher.get_grouped_opcodes(3))[:200], start=1):
        first, last = group[0], group[-1]
        body: list[str] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                body += [" " + line for line in old[i1:i2]]
            else:
                body += ["-" + line for line in old[i1:i2]] + ["+" + line for line in new[j1:j2]]
        text, _ = redact("".join(line if line.endswith("\n") else line + "\n" for line in body))
        out.append(
            Hunk(
                hunk_id=f"h{index}",
                old_start=first[1] + 1,
                old_lines=last[2] - first[1],
                new_start=first[3] + 1,
                new_lines=last[4] - first[3],
                preview=text[:2048],
            )
        )
    return tuple(out)


def _unified(path_a: str, path_b: str, old: list[str], new: list[str]) -> tuple[str, int]:
    text = "".join(difflib.unified_diff(old, new, fromfile=path_a, tofile=path_b, n=3))
    return redact(text if text.endswith("\n") or not text else text + "\n")


def _counts(old: list[str], new: list[str]) -> tuple[int, int]:
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if tag != "equal":
            removed += i2 - i1
            added += j2 - j1
    return added, removed


def _plan(
    changes: list[FileChangeV1],
    files: list[PlannedFile],
    previews: list[tuple[str, int]],
) -> EditPlan:
    added = sum(f.added for f in files)
    removed = sum(f.removed for f in files)
    names = ", ".join(f.path for f in files[:3]) + (
        f" and {len(files) - 3} more" if len(files) > 3 else ""
    )
    return EditPlan(
        changes=tuple(changes),
        files=tuple(files),
        digest=change_digest(changes),
        summary=f"{len(files)} file{'s' if len(files) != 1 else ''} ({names}): +{added} -{removed}",
        preview=truncate_head_tail("\n".join(text for text, _ in previews), MAX_PREVIEW_BYTES).text,
        redactions=sum(count for _, count in previews),
    )


def conflict_result(
    path: str, expected: str | None, actual: str | None, current: bytes | None, near: str = ""
) -> AgentToolResultV1:
    """A stale edit: say what changed and show the current content around the edit so it can be redone."""
    head = (
        f"Conflict: {path} is not what you edited. "
        + (f"You expected sha256 {expected}, " if expected else "You expected it to be absent, ")
        + (f"it is now sha256 {actual}." if actual else "it does not exist now.")
    )
    excerpt = ""
    if current is not None:
        try:
            lines = current.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            lines = []
        center = 0
        first = next((ln.strip() for ln in near.splitlines() if ln.strip()), "")
        if first:
            containing = [i for i, ln in enumerate(lines) if first in ln]
            if containing:
                center = containing[0]
            else:
                close = difflib.get_close_matches(first, lines, n=1, cutoff=0.5)
                center = lines.index(close[0]) if close else 0
        start = max(center - 8, 0)
        window = lines[start : start + 40]
        excerpt = "\nCurrent content" + (
            f" (lines {start + 1}-{start + len(window)} of {len(lines)}):\n"
            if window
            else ": empty\n"
        )
        excerpt += "\n".join(f"{start + i + 1:>6}\t{line}" for i, line in enumerate(window))
    text, count = redact(head + excerpt)
    bounded = truncate_head_tail(text, MAX_EXCERPT_BYTES)
    return AgentToolResultV1(
        status="invalid",
        summary=head[:1024],
        content=bounded.text,
        truncated=bounded.truncated,
        bytes_total=bounded.bytes_total,
        redactions=count,
        data={"conflicts": [{"path": path, "expected_hash": expected, "actual_hash": actual}]},
        error_code="workspace_conflict",
    )


class EditTool(Tool):
    """An edit planned first, then applied. ``run`` does both; the runner calls them apart."""

    tool_class = "edit"
    max_argument_bytes = 3 * MAX_PATCH_BYTES  # a JSON-escaped patch can be larger than its text

    async def prepare(self, ctx: ToolContext, args: AgentToolArgs) -> EditPlan | AgentToolResultV1:
        raise NotImplementedError

    @staticmethod
    def request_digest(name: str, args: AgentToolArgs) -> str:
        return semantic_digest({"tool": name, "args": args.model_dump(mode="json")})

    async def already_applied(
        self, ctx: ToolContext, args: AgentToolArgs
    ) -> AgentToolResultV1 | None:
        """If this exact request was applied and the workspace still has its result, say so."""
        if ctx.workspace is None:
            return None
        found = await ctx.workspace.find_applied(self.request_digest(self.name, args))
        if found is None:
            return None
        return succeed(
            f"No change: these edits were already applied (workspace revision {found.revision}).",
            summary="already applied",
            data={"revision": found.revision, "replayed": True},
        )

    async def run(self, ctx: ToolContext, args: AgentToolArgs) -> AgentToolResultV1:
        if (replay := await self.already_applied(ctx, args)) is not None:
            return replay
        plan = await self.prepare(ctx, args)
        if isinstance(plan, AgentToolResultV1):
            return plan
        return await self.apply(ctx, plan, request_digest=self.request_digest(self.name, args))

    async def apply(
        self, ctx: ToolContext, plan: EditPlan, request_digest: str | None = None
    ) -> AgentToolResultV1:
        """Write a plan. Applying the same plan again changes nothing and says so."""
        if ctx.workspace is None:
            return fail("unavailable", "no_workspace", "Editing is not available for this session.")
        try:
            applied = await ctx.workspace.apply(plan.changes, request_digest=request_digest)
        except WorkspaceConflict as conflict:
            current = None
            if conflict.actual is not None:
                try:
                    current = await ctx.sandbox.read_bytes(ctx.handle, conflict.path)
                except (WorkspaceFileNotFound, PathRejected):
                    current = None
            return conflict_result(conflict.path, conflict.expected, conflict.actual, current)
        body = (
            "No change: these edits were already applied.\n"
            if applied.replayed
            else f"Applied (workspace revision {applied.revision}).\n"
        ) + "\n".join(
            f"{f.change:<7} {f.path}  +{f.added} -{f.removed}"
            + (f"  (from {f.renamed_from})" if f.renamed_from else "")
            + (f"  sha256 {f.after_hash}" if f.after_hash else "")
            for f in plan.files
        )
        return succeed(
            body + ("\n\n" + plan.preview if plan.preview and not applied.replayed else ""),
            summary=("already applied: " if applied.replayed else "applied: ") + plan.summary,
            already_redacted=plan.redactions,
            data={
                "revision": applied.revision,
                "replayed": applied.replayed,
                "digest": plan.digest,
                "files": [
                    {"path": f.path, "before": f.before_hash, "after": f.after_hash}
                    for f in plan.files[:8]
                ],
            },
        )


# --- apply_patch ----------------------------------------------------------------------------------


class PatchEdit(AgentToolArgs):
    path: Annotated[str, Field(min_length=1, max_length=1024)]
    expected_hash: Hash
    search: Annotated[str, Field(min_length=1, max_length=200_000)]
    replace: Annotated[str, Field(max_length=200_000)]
    occurrence: Annotated[int, Field(ge=1, le=10_000)] | None = None


class ApplyPatchArgs(AgentToolArgs):
    edits: Annotated[list[PatchEdit], Field(min_length=1, max_length=50)]


def _find_all(text: str, needle: str) -> list[int]:
    found, start = [], 0
    while (at := text.find(needle, start)) != -1:
        found.append(at)
        start = at + len(needle)
    return found


class ApplyPatch(EditTool):
    name = "apply_patch"
    description = (
        "Change existing files with SEARCH/REPLACE edits. Each edit names the file, the sha256 you saw "
        "when you read it, the exact text to find (`search`) and its replacement (`replace`). `search` must "
        "match exactly once, or give `occurrence` (1 is the first). Several edits to one file apply in order "
        "and share one hash. All edits apply together or none do. To create a file use create_file."
    )
    args_model = ApplyPatchArgs

    async def prepare(self, ctx: ToolContext, args: ApplyPatchArgs) -> EditPlan | AgentToolResultV1:  # type: ignore[override]
        if sum(len(e.search) + len(e.replace) for e in args.edits) > MAX_PATCH_BYTES:
            return fail("invalid", "patch_too_large", "The patch is larger than 1 MiB; split it.")
        grouped: dict[str, list[PatchEdit]] = {}
        for edit in args.edits:
            try:
                path = normalize_workspace_path(edit.path)
            except PathRejected:
                return fail("denied", "path_rejected", "That path is not allowed in the workspace.")
            if path in grouped and grouped[path][0].expected_hash != edit.expected_hash:
                return fail(
                    "invalid",
                    "inconsistent_hash",
                    f"Edits to {path} name different hashes; read the file once and use one hash.",
                )
            grouped.setdefault(path, []).append(edit)
        if any(is_sensitive(p) for p in grouped):
            return fail("denied", "sensitive_path", "Credential files are not edited by the agent.")
        changes: list[FileChangeV1] = []
        files: list[PlannedFile] = []
        previews: list[tuple[str, int]] = []
        failures: list[str] = []
        first_conflict: AgentToolResultV1 | None = None
        for path, edits in grouped.items():
            try:
                data = await ctx.sandbox.read_bytes(ctx.handle, path)
            except WorkspaceFileNotFound:
                failures.append(f"{path}: no such file (use create_file to make a new one)")
                continue
            actual = content_hash(data)
            if actual != edits[0].expected_hash:
                first_conflict = first_conflict or conflict_result(
                    path, edits[0].expected_hash, actual, data, edits[0].search
                )
                continue
            try:
                original = data.decode("utf-8")
            except UnicodeDecodeError:
                failures.append(f"{path}: not a UTF-8 text file")
                continue
            crlf = "\r\n" in original and original.count("\r\n") == original.count("\n")
            text = original
            broken = False
            for number, edit in enumerate(edits, start=1):
                search, replace = (
                    (edit.search.replace("\n", "\r\n"), edit.replace.replace("\n", "\r\n"))
                    if crlf and "\r\n" not in edit.search
                    else (edit.search, edit.replace)
                )
                where = _find_all(text, search)
                if edit.occurrence is not None:
                    if edit.occurrence > len(where):
                        failures.append(
                            f"{path} edit {number}: occurrence {edit.occurrence} requested but the text appears {len(where)} time(s)"
                        )
                        broken = True
                        break
                    at = where[edit.occurrence - 1]
                elif len(where) == 1:
                    at = where[0]
                elif not where:
                    first_line = next((ln for ln in edit.search.splitlines() if ln.strip()), "")
                    near = difflib.get_close_matches(
                        first_line, original.splitlines(), n=1, cutoff=0.6
                    )
                    hint = (
                        f" The closest existing line is {near[0].strip()[:120]!r}." if near else ""
                    )
                    failures.append(
                        f"{path} edit {number}: the search text was not found (matching is exact, including whitespace).{hint}"
                    )
                    broken = True
                    break
                else:
                    lines_at = ", ".join(str(text.count("\n", 0, w) + 1) for w in where[:5])
                    failures.append(
                        f"{path} edit {number}: the search text appears {len(where)} times (lines {lines_at}); add context or set occurrence"
                    )
                    broken = True
                    break
                text = text[:at] + replace + text[at + len(search) :]
            if broken:
                continue
            if text == original:
                failures.append(f"{path}: the edits change nothing")
                continue
            new = text.encode()
            if len(new) > MAX_FILE_BYTES:
                failures.append(f"{path}: the result exceeds the file size limit")
                continue
            old_lines, new_lines = _lines(data), _lines(new)
            added, removed = _counts(old_lines, new_lines)
            changes.append(FileChangeV1(op="write", path=path, content=new, expected_hash=actual))
            files.append(
                PlannedFile(
                    path,
                    "modify",
                    actual,
                    content_hash(new),
                    _hunks(old_lines, new_lines),
                    None,
                    added,
                    removed,
                )
            )
            previews.append(_unified(f"a/{path}", f"b/{path}", old_lines, new_lines))
        if first_conflict is not None and not failures:
            return first_conflict
        if failures or first_conflict is not None:
            shown = "\n".join(failures[:MAX_FAILURES_SHOWN])
            extra = (
                "\n(other files in the patch have a stale hash; re-read them)"
                if first_conflict is not None
                else ""
            )
            return AgentToolResultV1(
                status="invalid",
                summary="The patch was not applied; no file changed.",
                content="The patch was not applied and no file changed.\n" + shown + extra,
                bytes_total=len(shown),
                error_code="patch_failed",
                data={"failures": len(failures), "conflict": first_conflict is not None},
            )
        return _plan(changes, files, previews)


# --- create_file, delete_path, move_path ----------------------------------------------------------


class CreateFileArgs(AgentToolArgs):
    path: Annotated[str, Field(min_length=1, max_length=1024)]
    content: Annotated[str, Field(max_length=MAX_PATCH_BYTES)]


class CreateFile(EditTool):
    name = "create_file"
    description = "Create a new text file with the given content. Fails if the file already exists with different content."
    args_model = CreateFileArgs

    async def prepare(self, ctx: ToolContext, args: CreateFileArgs) -> EditPlan | AgentToolResultV1:  # type: ignore[override]
        try:
            path = normalize_workspace_path(args.path)
        except PathRejected:
            return fail("denied", "path_rejected", "That path is not allowed in the workspace.")
        data = args.content.encode()
        existing = (await ctx.sandbox.file_hashes(ctx.handle)).get(path)
        if existing is not None and existing != content_hash(data):
            current = await ctx.sandbox.read_bytes(ctx.handle, path)
            return conflict_result(path, None, existing, current)
        lines = _lines(data)
        change = FileChangeV1(op="write", path=path, content=data, expected_hash=existing)
        planned = PlannedFile(
            path, "create", None, content_hash(data), _hunks([], lines), None, len(lines), 0
        )
        return _plan([change], [planned], [_unified("/dev/null", f"b/{path}", [], lines)])


class DeletePathArgs(AgentToolArgs):
    path: Annotated[str, Field(min_length=1, max_length=1024)]
    expected_hash: Hash


class DeletePath(EditTool):
    name = "delete_path"
    description = "Delete one file. Name the sha256 you saw when you read it. Directories are deleted file by file."
    args_model = DeletePathArgs

    async def prepare(self, ctx: ToolContext, args: DeletePathArgs) -> EditPlan | AgentToolResultV1:  # type: ignore[override]
        try:
            path = normalize_workspace_path(args.path)
        except PathRejected:
            return fail("denied", "path_rejected", "That path is not allowed in the workspace.")
        if is_sensitive(path):
            return fail(
                "denied", "sensitive_path", "Credential files are not deleted by the agent."
            )
        try:
            data = await ctx.sandbox.read_bytes(ctx.handle, path)
        except WorkspaceFileNotFound:
            return fail("invalid", "file_not_found", "There is no such file in the workspace.")
        actual = content_hash(data)
        if actual != args.expected_hash:
            return conflict_result(path, args.expected_hash, actual, data)
        try:
            old = _lines(data)
        except UnicodeDecodeError:
            old = []
        change = FileChangeV1(op="delete", path=path, expected_hash=actual)
        planned = PlannedFile(path, "delete", actual, None, _hunks(old, []), None, 0, len(old))
        return _plan([change], [planned], [_unified(f"a/{path}", "/dev/null", old, [])])


class MovePathArgs(AgentToolArgs):
    path: Annotated[str, Field(min_length=1, max_length=1024)]
    to: Annotated[str, Field(min_length=1, max_length=1024)]
    expected_hash: Hash


class MovePath(EditTool):
    name = "move_path"
    description = "Rename or move one file. Name the sha256 you saw when you read it. Fails if the destination exists."
    args_model = MovePathArgs

    async def prepare(self, ctx: ToolContext, args: MovePathArgs) -> EditPlan | AgentToolResultV1:  # type: ignore[override]
        try:
            source = normalize_workspace_path(args.path)
            dest = normalize_workspace_path(args.to)
        except PathRejected:
            return fail("denied", "path_rejected", "That path is not allowed in the workspace.")
        if is_sensitive(source) or is_sensitive(dest):
            return fail("denied", "sensitive_path", "Credential files are not moved by the agent.")
        try:
            data = await ctx.sandbox.read_bytes(ctx.handle, source)
        except WorkspaceFileNotFound:
            return fail("invalid", "file_not_found", "There is no such file in the workspace.")
        actual = content_hash(data)
        if actual != args.expected_hash:
            return conflict_result(source, args.expected_hash, actual, data)
        taken = (await ctx.sandbox.file_hashes(ctx.handle)).get(dest)
        if taken is not None and taken != actual:
            return fail(
                "invalid", "destination_exists", f"{dest} already exists; choose another name."
            )
        change = FileChangeV1(op="move", path=source, expected_hash=actual, move_to=dest)
        planned = PlannedFile(dest, "rename", actual, actual, (), source, 0, 0)
        return _plan([change], [planned], [(f"rename from {source}\nrename to {dest}\n", 0)])


__all__ = [
    "ApplyPatch",
    "CreateFile",
    "DeletePath",
    "EditPlan",
    "EditTool",
    "MovePath",
    "PlannedFile",
]


def edit_plan_to_json(plan: EditPlan) -> dict[str, object]:
    """A plan as journal data, so the exact approved change can be applied after a restart."""
    return {
        "changes": [
            {
                "op": c.op,
                "path": c.path,
                "expected_hash": c.expected_hash,
                "content_b64": base64.b64encode(c.content).decode()
                if c.content is not None
                else None,
                "move_to": c.move_to,
            }
            for c in plan.changes
        ],
        "files": [
            {
                "path": f.path,
                "change": f.change,
                "before_hash": f.before_hash,
                "after_hash": f.after_hash,
                "hunks": [h.model_dump(mode="json") for h in f.hunks],
                "renamed_from": f.renamed_from,
                "added": f.added,
                "removed": f.removed,
            }
            for f in plan.files
        ],
        "digest": plan.digest,
        "summary": plan.summary,
        "preview": plan.preview,
        "redactions": plan.redactions,
    }


def edit_plan_from_json(data: dict[str, Any]) -> EditPlan:
    return EditPlan(
        changes=tuple(
            FileChangeV1(
                op=c["op"],
                path=c["path"],
                expected_hash=c["expected_hash"],
                content=base64.b64decode(c["content_b64"])
                if c["content_b64"] is not None
                else None,
                move_to=c["move_to"],
            )
            for c in data["changes"]
        ),
        files=tuple(
            PlannedFile(
                path=f["path"],
                change=f["change"],
                before_hash=f["before_hash"],
                after_hash=f["after_hash"],
                hunks=tuple(Hunk.model_validate_json(json.dumps(h)) for h in f["hunks"]),
                renamed_from=f["renamed_from"],
                added=f["added"],
                removed=f["removed"],
            )
            for f in data["files"]
        ),
        digest=data["digest"],
        summary=data["summary"],
        preview=data["preview"],
        redactions=data["redactions"],
    )
