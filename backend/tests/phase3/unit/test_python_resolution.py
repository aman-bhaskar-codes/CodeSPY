import hashlib
from uuid import uuid4

import pytest
from app.analyzers.languages.python import extract
from app.analyzers.symbols.python import resolve_python
from app.contracts.structure import AnalysisBudget, AnalysisConfigV1, SourceUnitV1


def make_unit(path: str, source: str) -> SourceUnitV1:
    data = source.encode("utf-8")
    return SourceUnitV1(
        file_id=uuid4(),
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
        language="python",
    )


def test_python_local_call_resolution():
    main_src = """def helper():
    return 42

def run():
    return helper()
"""
    main_unit = make_unit("main.py", main_src)
    budget = AnalysisBudget()
    config = AnalysisConfigV1(toolchain_hash="0" * 64)
    obs = extract(main_unit, budget)

    resolutions, _ = resolve_python([obs], {main_unit.path: main_unit}, config, budget)

    call_resolutions = [r for r in resolutions if r.resolution_kind == "call_target"]
    assert len(call_resolutions) >= 1
    resolved_calls = [r for r in call_resolutions if r.status == "resolved"]
    assert len(resolved_calls) == 1
    assert resolved_calls[0].basis == "lexical"


def test_python_import_and_cross_module_resolution():
    lib_src = """def utility():
    return 100
"""
    app_src = """from .lib import utility

def main():
    return utility()
"""
    lib_unit = make_unit("pkg/lib.py", lib_src)
    app_unit = make_unit("pkg/app.py", app_src)

    sources = {lib_unit.path: lib_unit, app_unit.path: app_unit}
    budget = AnalysisBudget()
    config = AnalysisConfigV1(toolchain_hash="0" * 64)

    lib_obs = extract(lib_unit, budget)
    app_obs = extract(app_unit, budget)

    resolutions, _ = resolve_python([lib_obs, app_obs], sources, config, budget)

    import_res = [
        r for r in resolutions if r.resolution_kind == "import" and r.status == "resolved"
    ]
    assert len(import_res) >= 1
    assert import_res[0].basis == "import_alias"

    call_res = [
        r for r in resolutions if r.resolution_kind == "call_target" and r.status == "resolved"
    ]
    assert len(call_res) >= 1
    assert call_res[0].basis == "import_alias"


def test_python_unresolved_external():
    app_src = """import external_nonexistent_lib

def main():
    external_nonexistent_lib.do_something()
"""
    unit = make_unit("app.py", app_src)
    budget = AnalysisBudget()
    config = AnalysisConfigV1(toolchain_hash="0" * 64)
    obs = extract(unit, budget)

    resolutions, _ = resolve_python([obs], {unit.path: unit}, config, budget)
    assert any(r.status == "unresolved" and r.resolution_kind == "import" for r in resolutions)


def call_results(source):
    unit = make_unit("main.py", source)
    obs = extract(unit, AnalysisBudget())
    rows, _ = resolve_python(
        [obs], {unit.path: unit}, AnalysisConfigV1(toolchain_hash="a" * 64), AnalysisBudget()
    )
    return [r for r in rows if r.resolution_kind == "call_target"]


def test_parameter_and_later_assignment_prevent_false_unique_calls():
    for source in (
        "def target(): pass\ndef run(target): target()\n",
        "def target(): pass\ndef run():\n target()\n target = unknown\n",
        "def target(): pass\ntarget = unknown\ntarget()\n",
        "def outer():\n def secret(): pass\nsecret()\n",
        "class C:\n def target(): pass\n def run(self): target()\n",
    ):
        assert all(r.status != "resolved" for r in call_results(source))


def test_conditional_definitions_remain_ambiguous():
    rows = call_results(
        "if condition:\n def target(): pass\nelse:\n def target(): pass\ntarget()\n"
    )
    assert len(rows) == 1 and rows[0].status == "ambiguous"


def test_nested_lexical_scope_uses_nearest_definition():
    source = "def target(): pass\ndef outer():\n def target(): pass\n target()\n"
    unit = make_unit("main.py", source)
    obs = extract(unit, AnalysisBudget())
    rows, _ = resolve_python(
        [obs], {unit.path: unit}, AnalysisConfigV1(toolchain_hash="a" * 64), AnalysisBudget()
    )
    target = next(s for s in obs.symbols if s.name == "target" and ".outer." in s.qualified_name)
    assert next(r for r in rows if r.resolution_kind == "call_target").candidate_keys == (
        target.semantic_key,
    )


@pytest.mark.parametrize(
    "source",
    [
        "def f(x: factory()): pass\n",
        "def f() -> factory(): pass\n",
        "class C(metaclass=factory()): pass\n",
        "def f(x: Annotated[str, Field()]): pass\n",
    ],
)
def test_annotation_and_class_keyword_calls_have_safe_scope(source):
    assert call_results(source)


def test_unaliased_dotted_import_keeps_package_identity():
    files = {
        "main.py": "import pkg.mod\npkg.f()\n",
        "pkg/__init__.py": "def f(): pass\n",
        "pkg/mod.py": "def f(): pass\n",
    }
    units = {path: make_unit(path, text) for path, text in files.items()}
    obs = [extract(unit, AnalysisBudget()) for unit in units.values()]
    rows, _ = resolve_python(
        obs, units, AnalysisConfigV1(toolchain_hash="a" * 64), AnalysisBudget()
    )
    key = next(
        s.semantic_key
        for o in obs
        if o.path == "pkg/__init__.py"
        for s in o.symbols
        if s.name == "f"
    )
    assert next(r for r in rows if r.resolution_kind == "call_target").candidate_keys == (key,)
