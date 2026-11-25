"""The agent's read-only tools. ``read_tools()`` is the set offered in plan mode."""

from app.services.agent.tools.edit_tools import ApplyPatch, CreateFile, DeletePath, MovePath
from app.services.agent.tools.exec_tools import RunCommand, RunTests
from app.services.agent.tools.git_tools import GitDiff, GitStatus
from app.services.agent.tools.registry import ToolRegistry
from app.services.agent.tools.search_tools import FindSymbol, SearchSemantic
from app.services.agent.tools.workspace_tools import Glob, Grep, ListDir, ReadFile


def read_tools() -> ToolRegistry:
    """list_dir, read_file, glob, grep, find_symbol, search_semantic, git_status, git_diff."""
    return ToolRegistry(
        [
            ListDir(),
            ReadFile(),
            Glob(),
            Grep(),
            FindSymbol(),
            SearchSemantic(),
            GitStatus(),
            GitDiff(),
        ]
    )


def edit_tools() -> ToolRegistry:
    """apply_patch, create_file, delete_path, move_path."""
    return ToolRegistry([ApplyPatch(), CreateFile(), DeletePath(), MovePath()])


def exec_tools() -> ToolRegistry:
    """run_command, run_tests."""
    return ToolRegistry([RunCommand(), RunTests()])


def tools_for_mode(mode: str) -> ToolRegistry:
    """Plan mode offers the read tools only; act mode adds the edit and exec tools."""
    act = mode == "act"
    return ToolRegistry(
        [*read_tools().tools(), *(edit_tools().tools() + exec_tools().tools() if act else [])]
    )
