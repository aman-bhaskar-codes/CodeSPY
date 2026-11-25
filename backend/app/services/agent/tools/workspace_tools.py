# ruff: noqa: E501
"""Read-only tools over the sandbox workspace: list, read, glob and grep."""

from collections import defaultdict
from typing import Annotated

from pydantic import Field

from app.contracts.agent_events import EvidenceRef, SourceRangeProvenance
from app.contracts.agent_tools import AgentToolArgs, AgentToolResultV1
from app.contracts.workspace import ExecRequestV1
from app.ports.sandbox import MAX_LIST_ENTRIES, normalize_workspace_path
from app.services.agent.tools.base import Tool, ToolContext, fail, succeed
from app.services.agent.tools.text import evidence_id, glob_regex, is_sensitive, redact

READ_LINES = 800
GREP_SECONDS = 10
GREP_LINE_CHARS = 300


def _size(n: int) -> str:
    return (
        f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB" if n < 1 << 20 else f"{n / (1 << 20):.1f} MB"
    )


class ListDirArgs(AgentToolArgs):
    path: Annotated[str, Field(max_length=1024)] = "."
    depth: Annotated[int, Field(ge=1, le=3)] = 1


class ListDir(Tool):
    name = "list_dir"
    description = (
        "List a directory of the workspace: files with sizes and subdirectories with file counts, "
        "down to `depth` levels (1 to 3)."
    )
    args_model = ListDirArgs

    async def run(self, ctx: ToolContext, args: ListDirArgs) -> AgentToolResultV1:
        root = normalize_workspace_path(args.path, allow_root=True)
        listing = await ctx.sandbox.list_files(ctx.handle, root, MAX_LIST_ENTRIES)
        if any(e.path == root for e in listing.entries):
            return fail("invalid", "not_a_directory", f"{root} is a file; use read_file.")
        prefix = "" if root == "." else root + "/"
        dir_counts: dict[str, int] = defaultdict(int)
        rows: list[tuple[str, str]] = []  # (sort key, line); a directory sorts before its files
        for entry in listing.entries:
            rel = entry.path[len(prefix) :]
            parts = rel.split("/")
            for depth in range(1, min(len(parts), args.depth + 1)):
                dir_counts["/".join(parts[:depth])] += 1
            if len(parts) <= args.depth:
                note = "  (content withheld)" if is_sensitive(entry.path) else ""
                rows.append(
                    (rel, f"{'  ' * (len(parts) - 1)}{parts[-1]}  {_size(entry.size)}{note}")
                )
        for directory, count in dir_counts.items():
            rows.append(
                (
                    directory + "/",
                    f"{'  ' * directory.count('/')}{directory.rsplit('/', 1)[-1]}/  ({count} files)",
                )
            )
        out = sorted(rows)
        header = f"{root}: {len(listing.entries)} files{' (listing truncated)' if listing.truncated else ''}"
        if not out:
            return succeed(header + "\n(empty or no such directory)", summary=header)
        return succeed(
            header + "\n" + "\n".join(line for _, line in out),
            summary=header,
            partial=listing.truncated,
        )


class ReadFileArgs(AgentToolArgs):
    path: Annotated[str, Field(min_length=1, max_length=1024)]
    start_line: Annotated[int, Field(ge=1)] = 1
    end_line: Annotated[int, Field(ge=1)] | None = None


class ReadFile(Tool):
    name = "read_file"
    description = (
        "Read a text file of the workspace with line numbers (at most 800 lines per call). The result "
        "names the file's hash; an edit must quote that hash. Keys, credentials and .env files are "
        "withheld, and recognised secrets are replaced by markers."
    )
    args_model = ReadFileArgs

    async def run(self, ctx: ToolContext, args: ReadFileArgs) -> AgentToolResultV1:
        path = normalize_workspace_path(args.path)
        if is_sensitive(path):
            return fail(
                "denied", "sensitive_path", "That file holds credentials; its content is withheld."
            )
        end = args.end_line if args.end_line is not None else args.start_line + READ_LINES - 1
        clipped = end - args.start_line + 1 > READ_LINES
        end = min(end, args.start_line + READ_LINES - 1)
        piece = await ctx.sandbox.read_file(ctx.handle, path, args.start_line, end)
        text, secrets = redact(piece.content)
        lines = text.splitlines()
        numbered = "\n".join(f"{piece.start_line + i:>6}\t{line}" for i, line in enumerate(lines))
        last = piece.end_line if piece.end_line else piece.start_line - 1
        header = f"{path} (lines {piece.start_line}-{last} of {piece.total_lines}; sha256 {piece.file_hash})"
        evidence: tuple[EvidenceRef, ...] = ()
        if lines:
            evidence = (
                EvidenceRef(
                    evidence_id=evidence_id(path, piece.start_line, last, piece.file_hash),
                    provenance=SourceRangeProvenance(
                        path=path, start_line=piece.start_line, end_line=last
                    ),
                ),
            )
        return succeed(
            header + "\n" + numbered if numbered else header + "\n(no lines in that range)",
            summary=f"read {len(lines)} lines of {path}",
            partial=piece.truncated
            or clipped
            or (args.end_line is None and last < piece.total_lines),
            evidence=evidence,
            data={"path": path, "file_hash": piece.file_hash, "total_lines": piece.total_lines},
            already_redacted=secrets,
            redact_content=False,
        )


class GlobArgs(AgentToolArgs):
    pattern: Annotated[str, Field(min_length=1, max_length=256)]


class Glob(Tool):
    name = "glob"
    description = (
        "Find workspace files by path pattern: `*` stays within a directory, `**` crosses directories, "
        "`?` is one character. Example: `src/**/*.py`."
    )
    args_model = GlobArgs

    async def run(self, ctx: ToolContext, args: GlobArgs) -> AgentToolResultV1:
        if (
            args.pattern.startswith("/")
            or ".." in args.pattern.split("/")
            or "\x00" in args.pattern
        ):
            return fail("denied", "path_rejected", "That pattern leaves the workspace.")
        regex = glob_regex(args.pattern.removeprefix("./"))
        listing = await ctx.sandbox.list_files(ctx.handle, ".", MAX_LIST_ENTRIES)
        hits = [e.path for e in listing.entries if regex.match(e.path)]
        summary = f"{len(hits)} files match {args.pattern}"
        note = (
            "\n(the workspace listing was cut at 2000 files; narrow the pattern)"
            if listing.truncated
            else ""
        )
        return succeed(
            "\n".join(hits) + note if hits else summary + note,
            summary=summary,
            partial=listing.truncated,
        )


class GrepArgs(AgentToolArgs):
    pattern: Annotated[str, Field(min_length=1, max_length=512)]
    path_glob: Annotated[str, Field(max_length=256)] | None = None
    ignore_case: bool = False
    regex: bool = False
    max_hits: Annotated[int, Field(ge=1, le=200)] = 100


class Grep(Tool):
    name = "grep"
    description = (
        "Search workspace files for a literal string (or an extended regular expression when "
        "`regex` is true). Optionally limit to files matching `path_glob`. Returns `path:line: text`."
    )
    args_model = GrepArgs

    async def run(self, ctx: ToolContext, args: GrepArgs) -> AgentToolResultV1:
        if "\x00" in args.pattern:
            return fail("invalid", "invalid_arguments", "The pattern contains a NUL byte.")
        wanted = glob_regex(args.path_glob.removeprefix("./")) if args.path_glob else None
        argv = ["grep", "-rnIHZ", "--exclude-dir=.git", "-E" if args.regex else "-F"]
        if args.ignore_case:
            argv.append("-i")
        argv += ["-e", args.pattern, "--", "."]
        out: list[str] = []
        err: list[str] = []
        code: int | None = None
        async for chunk in ctx.sandbox.exec(
            ctx.handle, ExecRequestV1(argv=tuple(argv), timeout_s=GREP_SECONDS)
        ):
            if chunk.kind == "stdout":
                out.append(chunk.text)
            elif chunk.kind == "stderr":
                err.append(chunk.text)
            else:
                code = chunk.exit_code
                if chunk.timed_out:
                    return fail(
                        "timed_out",
                        "timed_out",
                        "The search took too long; narrow the pattern or path_glob.",
                    )
        if code not in (0, 1) and not out:
            return fail(
                "invalid",
                "invalid_pattern",
                "The pattern could not be used: " + ("".join(err).strip()[:200] or "unknown error"),
            )
        hits, withheld, evidence = [], 0, []
        for line in "".join(out).split("\n"):
            path, sep, rest = line.partition("\x00")
            digits, colon, text = rest.partition(":")
            if not sep or not colon or not digits.isdigit():
                continue
            path = path.removeprefix("./")
            if wanted is not None and not wanted.match(path):
                continue
            if is_sensitive(path):
                withheld += 1
                continue
            hits.append((path, int(digits), text[:GREP_LINE_CHARS]))
            if len(hits) >= args.max_hits:
                break
        for path, number, _ in hits[:20]:
            evidence.append(
                EvidenceRef(
                    evidence_id=evidence_id(path, number, args.pattern),
                    provenance=SourceRangeProvenance(path=path, start_line=number, end_line=number),
                )
            )
        body = "\n".join(f"{p}:{n}: {t}" for p, n, t in hits)
        notes = []
        if len(hits) >= args.max_hits:
            notes.append(f"stopped at {args.max_hits} matches")
        if withheld:
            notes.append(f"{withheld} matches in credential files were withheld")
        summary = f"{len(hits)} matches for {args.pattern[:60]!r}"
        return succeed(
            (body or "no matches") + ("\n(" + "; ".join(notes) + ")" if notes else ""),
            summary=summary,
            partial=len(hits) >= args.max_hits,
            evidence=evidence,
        )
