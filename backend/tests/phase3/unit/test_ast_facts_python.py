import hashlib
from uuid import uuid4

from app.analyzers.facts import extract_facts
from app.analyzers.positions import SourceIndex
from app.contracts.structure import AnalysisBudget, SourceUnitV1

SOURCE = '''"""doc"""
import os, sys as system
import a.b.c
from .pkg import helper as h, other
from .. import up
from typing import *

__all__ = ["Service", "run"]
MAX_RETRIES = 3
_hidden = 1


@decorator
@other.deco(arg=1)
class Service(Base, metaclass=Meta):
    LIMIT = 5

    async def start(self, host: str, port: int = 80, *args: int, flag=False, **kw) -> None:
        def local():
            pass

    def _internal(self):
        pass

    class Inner:
        def go(self): ...


def run(a, /, b, *, c: int = 1):
    return a


def __private():
    pass
'''


def make_unit(source: str, path: str = "pkg/mod.py") -> SourceUnitV1:
    data = source.encode("utf-8")
    return SourceUnitV1(
        file_id=uuid4(),
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
        language="python",
    )


def facts(source: str = SOURCE):
    return extract_facts(make_unit(source), AnalysisBudget(max_syntax_nodes=100000))


def test_symbols_signatures_bases_and_decorators():
    result = facts()
    assert result.status == "parsed" and result.error_nodes == 0
    by_name = {s.qualified_name: s for s in result.symbols}
    service = by_name["pkg.mod.Service"]
    assert service.kind == "class" and service.bases == ("Base",)
    assert service.decorators == ("decorator", "other.deco(arg=1)")
    start = by_name["pkg.mod.Service.start"]
    assert start.kind == "method" and start.modifiers == ("async",)
    assert start.container == "pkg.mod.Service"
    params = [(p.name, p.type, p.default, p.kind) for p in start.signature.params]
    assert params == [
        ("self", None, None, "positional"),
        ("host", "str", None, "positional"),
        ("port", "int", "80", "positional"),
        ("args", "int", None, "variadic"),
        ("flag", None, "False", "keyword_only"),
        ("kw", None, None, "variadic_keyword"),
    ]
    assert start.signature.return_type == "None"
    run = by_name["pkg.mod.run"]
    assert [(p.name, p.kind) for p in run.signature.params] == [
        ("a", "positional"),
        ("b", "positional"),
        ("c", "keyword_only"),
    ]
    assert by_name["pkg.mod.Service.Inner.go"].kind == "method"
    assert by_name["pkg.mod.MAX_RETRIES"].kind == "constant"
    assert by_name["pkg.mod.Service._internal"].visibility == "internal"
    assert by_name["pkg.mod.__private"].visibility == "private"
    assert "pkg.mod.Service.start.local" not in by_name  # local functions are not API surface
    assert "pkg.mod._hidden" not in by_name


def test_imports_are_structured():
    imports = [
        (i.kind, i.module, i.relative_level, [(n.name, n.alias) for n in i.names], i.alias)
        for i in facts().imports
    ]
    assert imports == [
        ("static", "os", 0, [], None),
        ("static", "sys", 0, [], "system"),
        ("static", "a.b.c", 0, [], None),
        ("from", "pkg", 1, [("helper", "h"), ("other", None)], None),
        ("from", "", 2, [("up", None)], None),
        ("wildcard", "typing", 0, [], None),
    ]


def test_all_list_defines_exports_otherwise_public_declarations():
    assert [(e.name, e.kind) for e in facts().exports] == [
        ("Service", "all_list"),
        ("run", "all_list"),
    ]
    implicit = facts("def public(): ...\ndef _private(): ...\nclass K: ...\nLIMIT = 1\n")
    assert [(e.name, e.kind) for e in implicit.exports] == [
        ("public", "declared"),
        ("K", "declared"),
        ("LIMIT", "declared"),
    ]


def test_every_range_cites_original_bytes_and_lines():
    unit = make_unit(SOURCE)
    result = extract_facts(unit, AnalysisBudget(max_syntax_nodes=100000))
    index = SourceIndex(unit.data)
    ranges = [s.range for s in result.symbols] + [i.range for i in result.imports]
    ranges += [e.range for e in result.exports]
    assert ranges
    for span in ranges:
        index.validate_range(span)
    service = next(s for s in result.symbols if s.name == "Service")
    assert unit.data[service.range.start_byte : service.range.end_byte].startswith(b"@decorator")
    assert service.range.start_line == 13


def test_output_is_deterministic():
    unit = make_unit(SOURCE)
    first = extract_facts(unit, AnalysisBudget(max_syntax_nodes=100000))
    second = extract_facts(unit, AnalysisBudget(max_syntax_nodes=100000))
    assert first.facts_sha256() == second.facts_sha256()


def test_syntax_errors_yield_partial_facts_not_a_crash():
    result = facts("def ok(a):\n    return a\n\ndef broken(:\n    pass\n")
    assert result.status == "partial" and result.error_nodes > 0
    assert any(d.code == "syntax_error" for d in result.diagnostics)
    assert any(s.name == "ok" for s in result.symbols)


def test_node_budget_is_enforced():
    import pytest

    with pytest.raises(TimeoutError, match="syntax_node_budget_exceeded"):
        extract_facts(make_unit(SOURCE), AnalysisBudget(max_syntax_nodes=10))
