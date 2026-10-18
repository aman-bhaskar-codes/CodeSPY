"""Shared synthetic fixture; no parser, upload, model or cloud calls."""

from scripts.phase1_fixture import batch_for, scenario

__all__ = ["batch_for", "scenario"]
