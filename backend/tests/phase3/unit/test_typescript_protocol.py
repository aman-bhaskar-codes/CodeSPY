import pytest
from app.adapters.typescript import TypeScriptProtocolError, validate_input, validate_result


def test_rejects_traversal_and_unbounded_protocol():
    for path in ("../a.ts", "/a.ts", "a\\b.ts", "__trusted/lib.es2022.d.ts"):
        with pytest.raises(TypeScriptProtocolError):
            validate_input({"schema_version": 1, "files": [{"path": path, "text": ""}]})
    with pytest.raises(TypeScriptProtocolError):
        validate_input({"schema_version": 1, "files": [], "max_sites": True})


def test_result_admission_and_utf16_bounds():
    request = {"schema_version": 1, "files": [{"path": "a.ts", "text": "😀\r\nf()"}]}
    row = {
        "path": "a.ts",
        "start": 4,
        "end": 5,
        "namespace": "value",
        "resolution_kind": "binding",
        "status": "resolved",
        "candidates": [{"path": "a.ts", "start": 4, "end": 5, "name": "f"}],
        "reason": "static_symbol",
        "basis": "compiler_symbol",
    }
    result = {"schema_version": 1, "resolutions": [row], "diagnostics": [], "truncated": False}
    assert validate_result(result, request) is result
    row["candidates"][0]["path"] = "/etc/passwd"
    with pytest.raises(TypeScriptProtocolError):
        validate_result(result, request)


def test_rejects_unknown_fields_and_inconsistent_status():
    request = {"schema_version": 1, "files": []}
    with pytest.raises(TypeScriptProtocolError):
        validate_result(
            {
                "schema_version": 1,
                "resolutions": [],
                "diagnostics": [],
                "truncated": False,
                "extra": 0,
            },
            request,
        )


def test_reassigned_function_is_not_a_unique_static_target():
    from app.adapters.typescript import TypeScriptAdapter

    result = TypeScriptAdapter().resolve(
        {
            "schema_version": 1,
            "files": [
                {
                    "path": "rebind.ts",
                    "text": (
                        "export {}; function original() {} original = unknownValue; original();"
                    ),
                }
            ],
        }
    )
    calls = [r for r in result["resolutions"] if r["resolution_kind"] == "call_target"]
    assert calls and all(r["status"] == "unresolved" for r in calls)


def test_sources_and_configs_have_independent_profile_budgets():
    from app.adapters.typescript import validate_input

    request = {
        "schema_version": 1,
        "files": [{"path": f"source_{i}.ts", "text": "export {};"} for i in range(128)],
        "configs": [{"path": f"tsconfig{i}.json", "text": "{}"} for i in range(32)],
    }
    assert validate_input(request) == request


def test_encoded_input_capacity_is_bounded_before_launch():
    import pytest
    from app.adapters.typescript import TypeScriptCapacityError, serialize_input

    request = {
        "schema_version": 1,
        "files": [{"path": f"file_{i}.ts", "text": "\\" * (1024 * 1024)} for i in range(11)],
    }
    with pytest.raises(TypeScriptCapacityError):
        serialize_input(request)
