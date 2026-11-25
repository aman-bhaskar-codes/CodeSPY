"""Offline, deterministic dependency graph over published AST facts."""

from app.analyzers.graph.build import FileEntry, build_graph

__all__ = ["FileEntry", "build_graph"]
