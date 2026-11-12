import hashlib
from uuid import uuid4

import pytest
from app.analyzers.facts import extract_facts
from app.analyzers.positions import SourceIndex
from app.contracts.structure import AnalysisBudget, SourceUnitV1

GO = """package server

import (
	"fmt"
	str "strings"
	. "math"
	_ "embed"
)
import "os"

const Max = 10
const (
	A, b = 1, 2
)
var Name string = "x"

type Server struct {
	Host string
	port int
	Base
	*Other
}
type Handler interface {
	Serve(w int) error
	io.Reader
}
type ID int
type Alias = string

func (s *Server) Start(a int, b ...string) (int, error) { return 0, nil }
func (s Server) name() string { return "" }
func New(host string) *Server { return nil }
func generic[T any](x T) T { return x }
func pair(a, b int) {}
"""

JAVA = """package com.ex;

import java.util.List;
import static java.lang.Math.max;
import java.io.*;

@Entity
@Table(name = "foo")
public class Foo<T> extends Bar implements Baz, Qux {
  private static final int MAX = 1;
  protected String name, other;
  int pkg;
  public Foo(int a) {}
  @Override
  public <R> List<R> go(String s, int... xs) throws Exception { return null; }
  public static class Inner {}
}

interface I extends J {
  int LIMIT = 3;
  void m();
  default int d() { return 1; }
}

public enum E { A, B; void helper() {} }

public record P(int x, String y) { }

abstract class Abs { abstract void run(); }
"""

RUST = """use std::collections::{HashMap, HashSet as Set};
use crate::a::b;
use super::*;
pub use self::inner::Thing;
use std::fmt as f;

mod inner { pub fn f() {} }

#[derive(Debug, Clone)]
pub struct S<T> { pub a: i32, b: T }
struct Tup(i32, String);
pub enum E { A, B(i32), C { x: u8 } }

pub trait Tr: Base + Send {
    fn m(&self) -> i32;
    fn d(&self) {}
}

impl<T> S<T> {
    pub fn new(a: i32) -> Self { todo!() }
    pub async fn run(&mut self, x: &str) -> Result<(), E> { Ok(()) }
    fn hidden(&self) {}
}
impl Tr for S<i32> { fn m(&self) -> i32 { 1 } }

pub const MAX: usize = 5;
static mut G: i32 = 1;
pub type R = Result<i32, E>;

#[inline]
pub unsafe fn free<T: Clone>(x: T) -> T { x }
pub(crate) fn crate_only() {}
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
    return [(p.name, p.type, p.kind) for p in symbol.signature.params]


def test_go_types_methods_fields_and_exported_names():
    _, result = facts(GO, "go", "svc/server/server.go")
    assert result.status == "parsed" and result.grammar == "tree-sitter-go"
    symbols = by_name(result)
    server = symbols["svc.server.Server"]
    assert server.kind == "struct" and server.bases == ("Base", "Other") and server.exported
    assert symbols["svc.server.Server.Host"].visibility == "public"
    assert symbols["svc.server.Server.port"].visibility == "internal"
    start = symbols["svc.server.Server.Start"]
    assert start.kind == "method" and start.modifiers == ("pointer_receiver",)
    assert params(start) == [("a", "int", "positional"), ("b", "string", "variadic")]
    assert start.signature.return_type == "(int, error)"
    assert symbols["svc.server.Server.name"].modifiers == ()  # value receiver
    handler = symbols["svc.server.Handler"]
    assert handler.kind == "interface" and handler.bases == ("io.Reader",)
    assert symbols["svc.server.Handler.Serve"].kind == "method"
    assert symbols["svc.server.ID"].kind == "type_alias"
    assert symbols["svc.server.ID"].bases == ("int",)
    assert symbols["svc.server.Alias"].signature.return_type == "string"
    assert symbols["svc.server.Max"].kind == "constant"
    assert symbols["svc.server.b"].exported is False and symbols["svc.server.A"].exported
    assert symbols["svc.server.Name"].kind == "variable"
    assert params(symbols["svc.server.pair"]) == [
        ("a", "int", "positional"),
        ("b", "int", "positional"),
    ]
    assert params(symbols["svc.server.generic"]) == [("x", "T", "positional")]
    assert [e.name for e in result.exports] == [
        "Max",
        "A",
        "Name",
        "Server",
        "Handler",
        "ID",
        "Alias",
        "New",
    ]
    assert not any(s.name in {"", ","} for s in result.symbols)


def test_go_imports_cover_alias_dot_blank_and_single_imports():
    _, result = facts(GO, "go", "svc/server/server.go")
    assert [(i.kind, i.module, i.alias) for i in result.imports] == [
        ("static", "fmt", None),
        ("static", "strings", "str"),
        ("wildcard", "math", None),
        ("static", "embed", "_"),
        ("static", "os", None),
    ]


def test_java_types_members_annotations_and_visibility():
    _, result = facts(JAVA, "java", "src/com/ex/Foo.java")
    assert result.status == "parsed" and result.grammar == "tree-sitter-java"
    symbols = by_name(result)
    foo = symbols["com.ex.Foo"]
    assert foo.kind == "class" and foo.bases == ("Bar",) and foo.implements == ("Baz", "Qux")
    assert foo.decorators == ("Entity", 'Table(name = "foo")') and foo.exported
    assert symbols["com.ex.Foo.MAX"].kind == "constant"
    assert symbols["com.ex.Foo.MAX"].visibility == "private"
    assert symbols["com.ex.Foo.name"].kind == symbols["com.ex.Foo.other"].kind == "field"
    assert symbols["com.ex.Foo.pkg"].visibility == "internal"
    ctor = symbols["com.ex.Foo.Foo"]
    assert ctor.modifiers == ("constructor",) and params(ctor) == [("a", "int", "positional")]
    method = symbols["com.ex.Foo.go"]
    assert method.decorators == ("Override",) and method.signature.return_type == "List<R>"
    assert params(method) == [("s", "String", "positional"), ("xs", "int", "variadic")]
    assert symbols["com.ex.Foo.Inner"].kind == "class"
    assert not symbols["com.ex.Foo.Inner"].exported
    iface = symbols["com.ex.I"]
    assert iface.kind == "interface" and iface.bases == ("J",) and iface.visibility == "internal"
    assert symbols["com.ex.I.m"].visibility == "public"  # interface members are public
    assert symbols["com.ex.I.d"].modifiers == ("default",)
    assert symbols["com.ex.I.LIMIT"].kind == "constant"
    assert symbols["com.ex.E"].kind == "enum"
    assert symbols["com.ex.E.A"].modifiers == ("enum_constant",)
    assert symbols["com.ex.E.helper"].kind == "method"
    assert symbols["com.ex.P"].modifiers == ("record",)
    assert {symbols["com.ex.P.x"].kind, symbols["com.ex.P.y"].kind} == {"field"}
    assert symbols["com.ex.Abs.run"].modifiers == ("abstract",)
    assert [e.name for e in result.exports] == ["Foo", "E", "P"]


def test_java_imports_static_wildcard_and_plain():
    _, result = facts(JAVA, "java", "src/com/ex/Foo.java")
    assert [(i.kind, i.module, [n.name for n in i.names]) for i in result.imports] == [
        ("static", "java.util.List", []),
        ("static", "java.lang.Math", ["max"]),
        ("wildcard", "java.io", []),
    ]


def test_rust_items_impls_traits_and_visibility():
    _, result = facts(RUST, "rust", "src/lib.rs")
    assert result.status == "parsed" and result.grammar == "tree-sitter-rust"
    symbols = by_name(result)
    s = symbols["src.lib.S"]
    assert s.kind == "struct" and s.decorators == ("derive(Debug, Clone)",) and s.exported
    assert s.implements == ("Tr",)  # from impl Tr for S<i32>
    assert symbols["src.lib.S.a"].visibility == "public"
    assert symbols["src.lib.S.b"].visibility == "private"
    assert symbols["src.lib.Tup.0"].signature.return_type == "i32"
    assert symbols["src.lib.E"].kind == "enum" and symbols["src.lib.E.B"].modifiers == ("variant",)
    tr = symbols["src.lib.Tr"]
    assert tr.kind == "trait" and tr.bases == ("Base", "Send")
    assert symbols["src.lib.Tr.m"].kind == "method"
    assert symbols["src.lib.Tr.m"].visibility == "public"
    run = symbols["src.lib.S.run"]
    assert run.kind == "method" and run.modifiers == ("async",)
    assert params(run) == [("self", "&mut self", "positional"), ("x", "&str", "positional")]
    assert run.signature.return_type == "Result<(), E>"
    assert symbols["src.lib.S.new"].signature.return_type == "Self"
    assert symbols["src.lib.S.hidden"].visibility == "private"
    assert symbols["src.lib.S.m"].visibility == "public"  # trait impl method
    assert symbols["src.lib.MAX"].kind == "constant" and symbols["src.lib.G"].modifiers == ("mut",)
    assert symbols["src.lib.R"].kind == "type_alias"
    free = symbols["src.lib.free"]
    assert free.modifiers == ("unsafe",) and free.decorators == ("inline",) and free.exported
    crate_only = symbols["src.lib.crate_only"]
    assert crate_only.visibility == "internal" and not crate_only.exported
    assert symbols["src.lib.inner"].kind == "namespace"
    assert symbols["src.lib.inner.f"].container == "src.lib.inner"
    assert [(e.kind, e.name) for e in result.exports] == [
        ("reexport", "Thing"),
        ("declared", "S"),
        ("declared", "E"),
        ("declared", "Tr"),
        ("declared", "MAX"),
        ("declared", "R"),
        ("declared", "free"),
    ]


def test_rust_use_declarations_are_structured():
    _, result = facts(RUST, "rust", "src/lib.rs")
    assert [
        (i.kind, i.module, [(n.name, n.alias) for n in i.names], i.alias) for i in result.imports
    ] == [
        ("static", "std::collections", [("HashMap", None), ("HashSet", "Set")], None),
        ("static", "crate::a::b", [], None),
        ("wildcard", "super", [], None),
        ("reexport", "self::inner::Thing", [], None),
        ("static", "std::fmt", [], "f"),
    ]


CASES = [
    ("go", "svc/server/server.go", GO),
    ("java", "src/com/ex/Foo.java", JAVA),
    ("rust", "src/lib.rs", RUST),
]


@pytest.mark.parametrize(("language", "path", "source"), CASES)
def test_every_range_cites_original_bytes_and_output_is_deterministic(language, path, source):
    unit, result = facts(source, language, path)
    index = SourceIndex(unit.data)
    ranges = [*result.symbols, *result.imports, *result.exports]
    assert ranges
    for item in ranges:
        index.validate_range(item.range)
    again = extract_facts(unit, AnalysisBudget(max_syntax_nodes=200000))
    assert result.facts_sha256() == again.facts_sha256()


@pytest.mark.parametrize(
    ("language", "path", "source"),
    [
        ("go", "a/a.go", "package a\nfunc ok() {}\nfunc broken( {\n"),
        ("java", "A.java", "class A { void ok() {} void bad( { }\n"),
        ("rust", "a.rs", "fn ok() {}\nfn bad( {\n"),
    ],
)
def test_syntax_errors_yield_partial_facts_not_a_crash(language, path, source):
    _, result = facts(source, language, path)
    assert result.status == "partial" and result.error_nodes > 0
    assert any(s.name == "ok" for s in result.symbols)


@pytest.mark.parametrize(("language", "path", "source"), CASES)
def test_node_budget_is_enforced(language, path, source):
    unit, _ = facts(source, language, path)
    with pytest.raises(TimeoutError, match="syntax_node_budget_exceeded"):
        extract_facts(unit, AnalysisBudget(max_syntax_nodes=2))
