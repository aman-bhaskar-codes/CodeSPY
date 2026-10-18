"""Destination allowlist checked before opening any test database connection."""

import re


def validate_target(*, host: str, port: int, database: str, marker: str, project: str) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"} or port != 55432:
        raise ValueError("test PostgreSQL must use loopback port 55432")
    if project != "codespy-phase1-tests" or not re.fullmatch(r"[0-9a-f]{32}", marker):
        raise ValueError("disposable ownership marker required")
    if not re.fullmatch(r"codespy_p1_test_[a-z0-9_]{1,60}", database):
        raise ValueError("test database name rejected")
