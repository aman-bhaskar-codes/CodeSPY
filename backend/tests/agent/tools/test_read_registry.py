"""The registry validates arguments, refuses traversal and withheld files, and never raises."""

import json
from uuid import uuid4

import pytest
from app.contracts.workspace import SandboxHandleV1
from app.services.agent.tools import read_tools

from tests.agent.tools.support import FILES, call, make_context, registry

TRAVERSAL = [
    "../etc/passwd",
    "/etc/passwd",
    "a/../../b",
    ".git/config",
    "src/.git/x",
    "a\\b",
    "a//b",
    "x\x00y",
    "./../x",
]


def test_the_read_tool_set_and_its_model_definitions():
    reads = read_tools()
    names = [s.name for s in reads.specs()]
    assert names == [
        "list_dir",
        "read_file",
        "glob",
        "grep",
        "find_symbol",
        "search_semantic",
        "git_status",
        "git_diff",
    ]
    assert {s.tool_class for s in reads.specs()} == {"read"}
    for definition in reads.model_definitions():
        assert definition.parameters_schema["type"] == "object"
        assert definition.parameters_schema.get("additionalProperties") is False
        assert definition.description


def test_tool_schemas_mark_required_fields():
    schemas = {s.name: s.parameters_schema for s in registry().specs()}
    assert schemas["read_file"]["required"] == ["path"]
    assert schemas["grep"]["required"] == ["pattern"]
    assert "required" not in schemas["list_dir"]


@pytest.mark.parametrize("bad", TRAVERSAL)
@pytest.mark.parametrize(("tool", "key"), [("read_file", "path"), ("list_dir", "path")])
async def test_traversal_is_denied_not_raised(tool, key, bad):
    ctx, sandbox = await make_context()
    result = await call(ctx, tool, **{key: bad})
    assert result.status in {"denied", "invalid"} and result.error_code in {
        "path_rejected",
        "invalid_arguments",
    }
    assert sandbox.reads == []  # the sandbox was never asked to open it


@pytest.mark.parametrize("bad", ["/etc/*", "../*", "a/../../*"])
async def test_glob_patterns_cannot_leave_the_workspace(bad):
    ctx, _ = await make_context()
    assert (await call(ctx, "glob", pattern=bad)).status == "denied"


async def test_credential_files_are_withheld_and_never_opened():
    ctx, sandbox = await make_context()
    for path in (".env", "certs/server.pem"):
        result = await call(ctx, "read_file", path=path)
        assert result.status == "denied" and result.error_code == "sensitive_path"
        assert "AKIA" not in result.content
    assert sandbox.reads == []


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("read_file", {}),
        ("read_file", {"path": "a", "extra": 1}),
        ("read_file", {"path": 5}),
        ("read_file", {"path": "a", "start_line": "1"}),
        ("read_file", {"path": "a", "start_line": True}),
        ("read_file", {"path": "a", "start_line": 0}),
        ("list_dir", {"depth": 4}),
        ("list_dir", {"depth": "2"}),
        ("grep", {"pattern": ""}),
        ("grep", {"pattern": "x", "max_hits": 201}),
        ("grep", {"pattern": "x", "regex": "yes"}),
        ("git_diff", {"paths": "src"}),
        ("git_diff", {"paths": ["a"] * 21}),
        ("git_diff", {"context": 11}),
        ("git_status", {"all": True}),
    ],
)
async def test_arguments_are_validated_strictly_without_coercion(tool, args):
    ctx, sandbox = await make_context()
    result = await call(ctx, tool, **args)
    assert result.status == "invalid" and result.error_code == "invalid_arguments"
    assert result.content.startswith("Invalid arguments.") and sandbox.reads == []


async def test_validation_messages_name_fields_but_never_echo_values():
    ctx, _ = await make_context()
    result = await call(ctx, "read_file", path="a", start_line="SECRET-VALUE-9f2")
    assert "start_line" in result.content and "SECRET-VALUE-9f2" not in result.content


async def test_unknown_tools_malformed_json_and_oversized_arguments_are_results():
    ctx, _ = await make_context()
    reg = registry()
    assert (await reg.execute(ctx, "rm_rf", "{}")).error_code == "unknown_tool"
    assert (await reg.execute(ctx, "read_file", "{not json")).error_code == "invalid_arguments"
    assert (await reg.execute(ctx, "read_file", "[1,2]")).error_code == "invalid_arguments"
    assert (
        await reg.execute(ctx, "read_file", "")
    ).error_code == "invalid_arguments"  # missing path
    assert (
        await reg.execute(ctx, "read_file", json.dumps({"path": "a" * 20000}))
    ).error_code == "arguments_too_large"
    assert (
        await reg.execute(ctx, "git_status", "")
    ).status == "ok"  # an empty argument string means no arguments


async def test_a_released_sandbox_is_unavailable_not_an_exception():
    ctx, sandbox = await make_context()
    await sandbox.release(ctx.handle)
    result = await call(ctx, "read_file", path="README.md")
    assert result.status == "unavailable" and result.error_code == "sandbox_unavailable"
    forged = ctx.__class__(
        sandbox=sandbox,
        handle=SandboxHandleV1(**{**ctx.handle.model_dump(), "id": uuid4()}),
    )
    assert (await call(forged, "list_dir")).status == "unavailable"


async def test_an_unexpected_failure_is_reported_without_its_cause():
    ctx, sandbox = await make_context()

    async def boom(*a, **kw):
        raise RuntimeError("secret internal detail /srv/app/key")

    sandbox.list_files = boom
    result = await call(ctx, "list_dir")
    assert result.status == "unavailable" and result.error_code == "tool_failed"
    assert "secret internal detail" not in result.model_dump_json()


async def test_results_serialise_for_the_event_stream():
    ctx, _ = await make_context()
    result = await call(ctx, "read_file", path="src/app.py")
    again = type(result).model_validate_json(result.model_dump_json())
    assert again == result and FILES["src/app.py"].decode().splitlines()[0] in result.content
