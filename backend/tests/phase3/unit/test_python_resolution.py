import hashlib
from uuid import uuid4

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
