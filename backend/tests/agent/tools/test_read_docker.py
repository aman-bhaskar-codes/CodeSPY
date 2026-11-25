# ruff: noqa: E501
"""The read tools against real containers: real grep, real files. Opt in with CODESPY_SANDBOX_IMAGE."""

from uuid import uuid4

import pytest
from app.contracts.workspace import SandboxSpecV1, SnapshotRefV1
from app.services.agent.tools.base import ToolContext

from tests.agent.sandbox.docker_support import IMAGE, Harness, requires_docker
from tests.agent.tools.support import AWS, CANARY, FILES, SECRETS, SNAPSHOT, call

pytestmark = [pytest.mark.sandbox, requires_docker]


@pytest.fixture
async def ctx():
    files = {**FILES, "src/Mixed.py": b"Hello = 1\nhello = 2\n-rf = 3\n"}
    harness = Harness({SNAPSHOT: files})
    org = uuid4()
    handle = await harness.sandbox.acquire(
        SandboxSpecV1(org_id=org, session_id=uuid4(), image=IMAGE)
    )
    await harness.sandbox.materialize(
        handle, SnapshotRefV1(org_id=org, repository_id=uuid4(), snapshot_id=SNAPSHOT)
    )
    yield ToolContext(sandbox=harness.sandbox, handle=handle)
    await harness.close()


async def test_grep_finds_literal_text_with_line_numbers(ctx):
    result = await call(ctx, "grep", pattern="def handle")
    assert result.content == "src/app.py:5: def handle():"
    assert result.status == "ok" and len(result.evidence) == 1


async def test_grep_regex_case_and_glob_options(ctx):
    assert (await call(ctx, "grep", pattern="hello")).content == "src/Mixed.py:2: hello = 2"
    both = await call(ctx, "grep", pattern="hello", ignore_case=True)
    assert both.content.splitlines() == ["src/Mixed.py:1: Hello = 1", "src/Mixed.py:2: hello = 2"]
    assert (
        await call(ctx, "grep", pattern="^def [a-z]+\\(", regex=True, path_glob="src/app.py")
    ).content.count("\n") == 1
    assert (await call(ctx, "grep", pattern="def", path_glob="docs/**")).content == "no matches"
    assert (
        await call(ctx, "grep", pattern="a.c")
    ).content == "no matches"  # literal by default: a dot is a dot


async def test_grep_treats_a_leading_dash_as_text_and_skips_binary_files(ctx):
    result = await call(ctx, "grep", pattern="-rf")
    assert result.content == "src/Mixed.py:3: -rf = 3"
    assert "data/blob.bin" not in (await call(ctx, "grep", pattern="a", regex=True)).content


async def test_grep_reports_an_invalid_regular_expression(ctx):
    result = await call(ctx, "grep", pattern="(", regex=True)
    assert result.status == "invalid" and result.error_code == "invalid_pattern"


async def test_grep_never_returns_credential_files_or_secrets(ctx):
    result = await call(ctx, "grep", pattern="AKIA")
    assert ".env" not in result.content.split("(")[0]
    assert AWS not in result.model_dump_json() and "withheld" in result.content
    canary = await call(ctx, "grep", pattern="canary")
    assert CANARY not in canary.model_dump_json() and canary.redactions == 1


async def test_a_catastrophic_pattern_cannot_hang_the_agent(ctx):
    result = await call(ctx, "grep", pattern="(a+)+$", regex=True)
    assert result.status in {"ok", "timed_out"}  # grep's matcher is linear; either way it returns


async def test_listing_reading_and_globbing_on_real_files(ctx):
    assert "src/  (4 files)" in (await call(ctx, "list_dir")).content
    read = await call(ctx, "read_file", path="src/config.py")
    assert all(secret not in read.model_dump_json() for secret in SECRETS)
    assert (
        read.content.splitlines()[-1].endswith("AFTER_THE_KEY = 1") and "     8\t" in read.content
    )
    assert (await call(ctx, "glob", pattern="src/*.py")).content.splitlines()[0] == "src/Mixed.py"
