"""The agent's read-only tools. ``read_tools()`` is the set offered in plan mode."""

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
