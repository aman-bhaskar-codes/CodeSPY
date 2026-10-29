"""Redacted operational visibility and telemetry for CodeSPY MCP server.

Strict privacy constraints:
- Stdout is reserved exclusively for JSON-RPC transport frames.
- All telemetry is written to stderr.
- Records ONLY:
  - Correlation identifier
  - Tool name and outcome category
  - Duration, queue wait, and response bytes
  - Coverage / omission counts
  - Cancellation, timeout, and authorization-denial counts
- Strictly FORBIDDEN from logging:
  - Tool arguments
  - Source excerpts
  - Queries
  - Memory text
  - Report bodies
  - Credentials or secrets
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any, Literal, TextIO

OutcomeCategory = Literal[
    "success",
    "scope_violation",
    "capacity_exceeded",
    "timeout",
    "cancellation",
    "invalid_parameters",
    "size_exceeded",
    "error",
]


class McpTelemetry:
    """Thread-safe redacted operational telemetry logger for MCP tool calls."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stderr
        self._lock = threading.Lock()
        self.total_calls: int = 0
        self.success_count: int = 0
        self.cancellation_count: int = 0
        self.timeout_count: int = 0
        self.authorization_denial_count: int = 0

    def record_call(
        self,
        *,
        correlation_id: str,
        tool_name: str,
        outcome_category: OutcomeCategory,
        duration_ms: float,
        queue_wait_ms: float,
        response_bytes: int,
        coverage_omissions_count: int = 0,
    ) -> dict[str, Any]:
        """Record a sanitized tool execution event to stderr without any data leakage."""
        with self._lock:
            self.total_calls += 1
            if outcome_category == "success":
                self.success_count += 1
            elif outcome_category == "scope_violation":
                self.authorization_denial_count += 1
            elif outcome_category == "timeout":
                self.timeout_count += 1
            elif outcome_category == "cancellation":
                self.cancellation_count += 1

            record: dict[str, Any] = {
                "event": "mcp_tool_call",
                "correlation_id": correlation_id,
                "tool_name": tool_name,
                "outcome_category": outcome_category,
                "duration_ms": round(max(0.0, duration_ms), 3),
                "queue_wait_ms": round(max(0.0, queue_wait_ms), 3),
                "response_bytes": max(0, response_bytes),
                "coverage_omissions_count": max(0, coverage_omissions_count),
                "total_calls": self.total_calls,
                "cancellation_count": self.cancellation_count,
                "timeout_count": self.timeout_count,
                "authorization_denial_count": self.authorization_denial_count,
            }

        line = json.dumps(record, separators=(",", ":")) + "\n"
        try:
            self.stream.write(line)
            self.stream.flush()
        except OSError:
            pass  # Broken pipe or closed stream on shutdown
        return record
