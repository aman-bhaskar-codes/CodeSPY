import hashlib
from uuid import uuid4

import pytest
from app.analyzers.facts import extract_facts
from app.analyzers.positions import SourceIndex
from app.contracts.structure import AnalysisBudget, SourceUnitV1

TS = """import def, { a as b, type T } from "mod";
import * as ns from "x";
import "side";
import type { U } from "y";
export { a as c, d } from "z";
export * from "w";
export default class K extends Base<T> implements I, J {
  private x: number = 1;
  static async m(a: string, b?: number, ...rest: any[]): Promise<void> {}
  get v(): string { return ""; }
}
export const f = (a: number, b = 2): string => "x";
export interface I extends P { m(): void; p: string }
export type Alias<T> = T | null;
export enum E { A, B }
const g = require("cjs");
const h = await import("dyn");
function plain<T>(x: T = 1 as any): void {}
const LIMIT = 5;
module.exports = { plain, other: 1 };
"""
JS = """const fs = require('fs');
const { a, b } = require('./ab');
class Repo extends Base {
  static count = 0;
  #secret = 1;
  constructor(db, { opt }, ...rest) { super(); }
  async *items() {}
  static of(x = 1) {}
}
function helper(a, b = 2) {}
const arrow = async x => x;
const handler = function named(req, res) {};
const MAX_SIZE = 10;
exports.helper = helper;
module.exports.extra = 1;
export default function main() {}
"""


def facts(source, language, path):
    data = source.encode()
    unit = SourceUnitV1(
        file_id=uuid4(),
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
        language=language,
    )
    return unit, extract_facts(unit, AnalysisBudget(max_syntax_nodes=200000))


def by_name(result):
    return {s.qualified_name: s for s in result.symbols}


def params(symbol):
    return [(p.name, p.type, p.default, p.kind, p.optional) for p in symbol.signature.params]


def test_typescript_symbols_signatures_heritage_and_modifiers():
    _, result = facts(TS, "typescript", "src/mod.ts")
    assert result.status == "parsed" and result.grammar == "tree-sitter-typescript"
    symbols = by_name(result)
    klass = symbols["src.mod.K"]
    assert klass.kind == "class" and klass.bases == ("Base",) and klass.implements == ("I", "J")
    assert klass.exported and klass.modifiers == ("export", "default")
    assert symbols["src.mod.K.x"].visibility == "private"
    assert symbols["src.mod.K.x"].signature.return_type == "number"
    method = symbols["src.mod.K.m"]
    assert method.kind == "method" and method.modifiers == ("static", "async")
    assert params(method) == [
        ("a", "string", None, "positional", False),
        ("b", "number", None, "positional", True),
        ("rest", "any[]", None, "variadic", False),
    ]
    assert method.signature.return_type == "Promise<void>"
    assert symbols["src.mod.K.v"].modifiers == ("getter",)
    arrow = symbols["src.mod.f"]
    assert arrow.kind == "function" and arrow.exported and arrow.signature.return_type == "string"
    assert params(arrow) == [
        ("a", "number", None, "positional", False),
        ("b", None, "2", "positional", False),
    ]
    assert symbols["src.mod.I"].kind == "interface" and symbols["src.mod.I"].bases == ("P",)
    assert symbols["src.mod.I.m"].kind == "method" and symbols["src.mod.I.p"].kind == "field"
    assert symbols["src.mod.Alias"].kind == "type_alias"
    assert symbols["src.mod.E"].kind == "enum"
    assert {symbols["src.mod.E.A"].kind, symbols["src.mod.E.B"].kind} == {"field"}
    assert params(symbols["src.mod.plain"]) == [("x", "T", "1 as any", "positional", False)]
    assert symbols["src.mod.LIMIT"].kind == "constant" and not symbols["src.mod.LIMIT"].exported
    assert "src.mod.g" not in symbols  # a plain require() binding is an import, not a symbol


def test_typescript_imports_are_structured_and_in_source_order():
    _, result = facts(TS, "typescript", "src/mod.ts")
    assert [
        (i.kind, i.module, [(n.name, n.alias) for n in i.names], i.alias, i.type_only)
        for i in result.imports
    ] == [
        ("static", "mod", [("default", "def"), ("a", "b"), ("T", None)], None, False),
        ("wildcard", "x", [], "ns", False),
        ("static", "side", [], None, False),
        ("static", "y", [("U", None)], None, True),
        ("reexport", "z", [("a", "c"), ("d", None)], None, False),
        ("reexport", "w", [], None, False),
        ("require", "cjs", [], None, False),
        ("dynamic", "dyn", [], None, False),
    ]
    starts = [i.range.start_byte for i in result.imports]
    assert starts == sorted(starts)


def test_typescript_exports_cover_default_named_reexport_and_commonjs():
    _, result = facts(TS, "typescript", "src/mod.ts")
    assert [(e.kind, e.name, e.from_module) for e in result.exports] == [
        ("reexport", "c", "z"),
        ("reexport", "d", "z"),
        ("reexport", "*", "w"),
        ("default", "default", None),
        ("declared", "f", None),
        ("declared", "I", None),
        ("declared", "Alias", None),
        ("declared", "E", None),
        ("default", "module.exports", None),
        ("declared", "plain", None),
        ("declared", "other", None),
    ]
    assert by_name(result)["src.mod.plain"].exported  # exported through module.exports


def test_javascript_commonjs_classes_and_functions():
    _, result = facts(JS, "javascript", "lib/util.js")
    assert result.grammar == "tree-sitter-javascript"
    symbols = by_name(result)
    repo = symbols["lib.util.Repo"]
    assert repo.kind == "class" and repo.bases == ("Base",) and repo.implements == ()
    assert symbols["lib.util.Repo.count"].modifiers == ("static",)
    assert symbols["lib.util.Repo.#secret"].visibility == "private"
    ctor = symbols["lib.util.Repo.constructor"]
    assert params(ctor) == [
        ("db", None, None, "positional", False),
        ("{ opt }", None, None, "positional", False),
        ("rest", None, None, "variadic", False),
    ]
    assert symbols["lib.util.Repo.items"].modifiers == ("async", "generator")
    assert params(symbols["lib.util.arrow"]) == [("x", None, None, "positional", False)]
    assert symbols["lib.util.arrow"].modifiers == ("async",)
    assert symbols["lib.util.handler"].kind == "function"
    assert symbols["lib.util.MAX_SIZE"].kind == "constant"
    assert symbols["lib.util.main"].modifiers == ("export", "default")
    assert [(i.kind, i.module) for i in result.imports] == [("require", "fs"), ("require", "./ab")]
    assert [(e.kind, e.name) for e in result.exports] == [
        ("declared", "helper"),
        ("declared", "extra"),
        ("default", "default"),
    ]
    assert symbols["lib.util.helper"].exported


def test_jsx_and_tsx_use_their_own_grammars():
    _, jsx = facts("export const App = ({ title }) => <div>{title}</div>;\n", "jsx", "ui/App.jsx")
    assert jsx.status == "parsed" and jsx.language == "jsx"
    assert [s.name for s in jsx.symbols] == ["App"]
    source = (
        "import React from 'react';\n"
        "export function Btn(props: { label: string }): JSX.Element { return <b/>; }\n"
    )
    _, tsx = facts(source, "tsx", "ui/Btn.tsx")
    assert tsx.status == "parsed" and tsx.grammar == "tree-sitter-typescript"
    assert tsx.symbols[0].signature.return_type == "JSX.Element"
    assert [(i.module, [(n.name, n.alias) for n in i.names]) for i in tsx.imports] == [
        ("react", [("default", "React")])
    ]


def test_namespaces_ambient_and_abstract_declarations():
    source = (
        "namespace N { export function f() {} }\n"
        "declare function d(x: number): void;\n"
        "abstract class A { abstract m(): void; protected readonly r = 1 }\n"
    )
    _, result = facts(source, "typescript", "x/ns.ts")
    symbols = by_name(result)
    assert symbols["x.ns.N"].kind == "namespace"
    assert symbols["x.ns.N.f"].exported and symbols["x.ns.N.f"].container == "x.ns.N"
    assert symbols["x.ns.d"].signature.return_type == "void"
    assert symbols["x.ns.A"].modifiers == ("abstract",)
    assert symbols["x.ns.A.m"].modifiers == ("abstract",)
    assert symbols["x.ns.A.r"].visibility == "protected"
    assert symbols["x.ns.A.r"].modifiers == ("readonly",)


@pytest.mark.parametrize(
    ("language", "path", "source"),
    [("typescript", "src/mod.ts", TS), ("javascript", "lib/util.js", JS)],
)
def test_every_range_cites_original_bytes_and_output_is_deterministic(language, path, source):
    unit, result = facts(source, language, path)
    index = SourceIndex(unit.data)
    for item in [*result.symbols, *result.imports, *result.exports]:
        index.validate_range(item.range)
    again = extract_facts(unit, AnalysisBudget(max_syntax_nodes=200000))
    assert result.facts_sha256() == again.facts_sha256()  # same unit, same digest


def test_syntax_errors_yield_partial_facts_not_a_crash():
    _, result = facts("export function ok(a: number) {}\nfunction bad( {\n", "typescript", "x.ts")
    assert result.status == "partial" and result.error_nodes > 0
    assert any(s.name == "ok" for s in result.symbols)


def test_node_budget_is_enforced():
    unit, _ = facts("const a = 1;\n", "javascript", "a.js")
    with pytest.raises(TimeoutError, match="syntax_node_budget_exceeded"):
        extract_facts(unit, AnalysisBudget(max_syntax_nodes=2))
