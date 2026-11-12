import hashlib
from uuid import uuid4

import pytest
from app.analyzers.facts import FACT_LANGUAGES, extract_facts
from app.analyzers.positions import SourceIndex
from app.analyzers.registry import SPECS
from app.contracts.structure import AnalysisBudget, SourceUnitV1

C = """#include <stdio.h>
#include "local.h"
#define MAX 10
#define SQ(x) ((x)*(x))

struct point { int x; int y; };
typedef struct node { int v; struct node *next; } node_t;
typedef struct { int a; } anon_t;
enum color { RED, GREEN = 2 };
typedef int (*cb_t)(int);

static int counter = 0;
const char *name;
int add(int a, int b);
static inline void helper(void) {}
int main(int argc, char **argv) { return 0; }
"""

CPP = """#include <vector>
#include "mine.hpp"

namespace ns {
class Base {};
class Foo : public Base, private Other {
 public:
  Foo(int a);
  virtual ~Foo();
  static int count;
  int run(int x) const override;
 private:
  int v;
};
}

template <typename T> class Box { T v; public: T get() const { return v; } };
int ns::Foo::run(int x) const { return x; }
using Alias = std::vector<int>;
struct S { int a; };
enum class E { A, B };
void free_fn(const std::string& s, int n = 3) {}
"""

RUBY = """require 'json'
require_relative './helper'
load 'x.rb'

module Api
  VERSION = '1'

  class User < Base
    include Comparable
    prepend Audit
    extend Forwardable
    attr_accessor :name, :age
    attr_reader :id

    def initialize(name, age = 1, *rest, key:, opt: 2, **kw, &blk); end
    def self.build(x); end
    def to_s; end

    private

    def secret; end
    attr_writer :hidden

    public

    def open_again; end
    def later; end
    private :later
    private def inline_private; end
  end
end

def top(a); end
"""

PHP = """<?php
namespace App\\Models;

use App\\Base;
use App\\Contracts\\{A, B as C};
use function App\\helper;
require_once 'x.php';

const MAX = 5;

interface I extends J { public function m(int $a): string; }
trait T { public function t() {} }

abstract class User extends Base implements I, K {
  use T;
  public const X = 1;
  private ?string $name = null;
  public static int $count = 0;
  public function __construct(private int $id, string ...$rest) {}
  abstract protected function run(array $a = [], ?int $b = null): void;
  public static function make(): static { return new static(1); }
}

function free(int $a): int { return $a; }
enum Suit: string { case H = 'h'; }
"""

SWIFT = """import Foundation
import struct Swift.Int

@objc public class Foo: Base, Proto {
  public var name: String
  private let id: Int = 1
  static var count = 0
  public init(name: String) { self.name = name }
  public func run(_ x: Int, with y: String = "a", rest: Int...) async throws -> Bool { return true }
  class func make() -> Foo { fatalError() }
}

public struct P: Codable { var x: Int }
enum E: Int { case a, b(Int) }
protocol Proto: Other { func m() -> Int; var p: Int { get } }
extension Foo: Equatable { func m() -> Int { 1 } }
actor Act { }
public func free(a: Int) -> Int { a }
typealias Alias = [Int]
let GLOBAL = 1
var counter = 0
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
    return [(p.name, p.type, p.default, p.kind) for p in symbol.signature.params]


def test_c_declarations_types_macros_and_includes():
    _, result = facts(C, "c", "src/sample.c")
    assert result.status == "parsed" and result.grammar == "tree-sitter-c"
    symbols = by_name(result)
    assert symbols["src.sample.MAX"].kind == "constant" and symbols["src.sample.MAX"].modifiers == (
        "macro",
    )
    assert symbols["src.sample.MAX"].signature.return_type == "10"
    assert symbols["src.sample.SQ"].kind == "function" and symbols["src.sample.SQ"].modifiers == (
        "macro",
    )
    assert not symbols["src.sample.SQ"].exported  # a macro is not a linkable symbol
    assert symbols["src.sample.point"].kind == "struct"
    assert symbols["src.sample.point.x"].signature.return_type == "int"
    assert symbols["src.sample.node"].kind == "struct"
    assert symbols["src.sample.node.next"].signature.return_type == "struct node*"
    assert symbols["src.sample.node_t"].kind == "type_alias" and symbols[
        "src.sample.node_t"
    ].bases == ("node",)
    assert symbols["src.sample.anon_t"].kind == "struct"  # typedef of an anonymous struct
    assert sum(s.qualified_name == "src.sample.anon_t" for s in result.symbols) == 1
    assert symbols["src.sample.color"].kind == "enum"
    assert symbols["src.sample.color.GREEN"].modifiers == ("enumerator",)
    assert symbols["src.sample.cb_t"].signature.return_type == "int (*cb_t)(int)"
    counter = symbols["src.sample.counter"]
    assert counter.kind == "variable" and counter.visibility == "internal"
    assert symbols["src.sample.name"].kind == "variable"  # pointer to const, not a constant
    assert symbols["src.sample.name"].signature.return_type == "char*"
    add = symbols["src.sample.add"]
    assert add.modifiers == ("prototype",) and params(add) == [
        ("a", "int", None, "positional"),
        ("b", "int", None, "positional"),
    ]
    helper = symbols["src.sample.helper"]
    assert helper.modifiers == ("static", "inline") and helper.visibility == "internal"
    assert params(helper) == []  # f(void) has no parameters
    main = symbols["src.sample.main"]
    assert params(main) == [
        ("argc", "int", None, "positional"),
        ("argv", "char**", None, "positional"),
    ]
    assert [(i.module, i.relative_level) for i in result.imports] == [
        ("stdio.h", 0),
        ("local.h", 1),
    ]
    assert [e.name for e in result.exports] == ["name", "main"]  # .c: external linkage only


def test_c_headers_export_their_declarations():
    _, result = facts(
        "struct point { int x; };\nint add(int a, int b);\nstatic int hidden;\n", "c", "inc/api.h"
    )
    assert [e.name for e in result.exports] == ["point", "add"]


def test_cpp_namespaces_classes_templates_and_qualified_methods():
    _, result = facts(CPP, "cpp", "src/sample.cpp")
    assert result.status == "parsed" and result.grammar == "tree-sitter-cpp"
    symbols = by_name(result)
    assert symbols["src.sample.ns"].kind == "namespace"
    foo = symbols["src.sample.ns.Foo"]
    assert foo.kind == "class" and foo.bases == ("Base", "Other")
    assert symbols["src.sample.ns.Foo.Foo"].modifiers == ("constructor", "declaration")
    assert symbols["src.sample.ns.Foo.~Foo"].modifiers == ("virtual", "destructor", "declaration")
    assert symbols["src.sample.ns.Foo.count"].kind == "field" and symbols[
        "src.sample.ns.Foo.count"
    ].modifiers == ("static",)
    assert symbols["src.sample.ns.Foo.v"].visibility == "private"
    declared = [s for s in result.symbols if s.qualified_name == "src.sample.ns.Foo.run"]
    assert [s.modifiers for s in declared] == [("const", "override", "declaration"), ("const",)]
    assert all(
        s.kind == "method" for s in declared
    )  # declaration in class + out-of-class definition
    box = symbols["src.sample.Box"]
    assert box.kind == "class" and box.modifiers == ("template",)
    assert symbols["src.sample.Box.v"].modifiers == ()  # the flag is not inherited by members
    assert symbols["src.sample.Box.get"].modifiers == ("const",)
    assert symbols["src.sample.Alias"].signature.return_type == "std::vector<int>"
    assert symbols["src.sample.E"].kind == "enum"
    free = symbols["src.sample.free_fn"]
    assert params(free) == [
        ("s", "std::string&", None, "positional"),
        ("n", "int", "3", "positional"),
    ]
    assert [(i.module, i.relative_level) for i in result.imports] == [
        ("vector", 0),
        ("mine.hpp", 1),
    ]


def test_ruby_modules_classes_mixins_visibility_and_requires():
    _, result = facts(RUBY, "ruby", "lib/api.rb")
    assert result.status == "parsed" and result.grammar == "tree-sitter-ruby"
    symbols = by_name(result)
    assert symbols["lib.api.Api"].kind == "namespace" and symbols["lib.api.Api"].exported
    assert symbols["lib.api.Api.VERSION"].kind == "constant"
    user = symbols["lib.api.Api.User"]
    assert user.kind == "class" and user.bases == ("Base",)
    assert user.implements == ("Comparable", "Audit") and user.decorators == ("extend Forwardable",)
    assert [(s.name, s.modifiers) for s in result.symbols if s.kind == "field"] == [
        ("name", ("accessor",)),
        ("age", ("accessor",)),
        ("id", ("reader",)),
        ("hidden", ("writer",)),
    ]
    assert symbols["lib.api.Api.User.hidden"].visibility == "private"  # after bare `private`
    init = symbols["lib.api.Api.User.initialize"]
    assert init.visibility == "private"
    assert params(init) == [
        ("name", None, None, "positional"),
        ("age", None, "1", "positional"),
        ("rest", None, None, "variadic"),
        ("key", None, None, "keyword_only"),
        ("opt", None, "2", "keyword_only"),
        ("kw", None, None, "variadic_keyword"),
        ("&blk", None, None, "positional"),
    ]
    assert symbols["lib.api.Api.User.build"].modifiers == ("singleton",)
    visibility = {n: symbols[f"lib.api.Api.User.{n}"].visibility for n in
                  ("to_s", "secret", "open_again", "later", "inline_private")}  # fmt: skip
    assert visibility == {
        "to_s": "public",
        "secret": "private",
        "open_again": "public",
        "later": "private",  # private :later
        "inline_private": "private",  # private def ...
    }
    assert symbols["lib.api.top"].kind == "function" and symbols["lib.api.top"].exported
    assert [(i.kind, i.module, i.relative_level) for i in result.imports] == [
        ("require", "json", 0),
        ("require", "./helper", 1),
        ("require", "x.rb", 0),
    ]


def test_php_namespaces_types_traits_enums_and_use():
    _, result = facts(PHP, "php", "src/Models/User.php")
    assert result.status == "parsed" and result.grammar == "tree-sitter-php"
    symbols = by_name(result)
    assert symbols["App.Models.MAX"].kind == "constant"
    iface = symbols["App.Models.I"]
    assert iface.kind == "interface" and iface.bases == ("J",)
    assert symbols["App.Models.T"].kind == "trait"
    user = symbols["App.Models.User"]
    assert user.kind == "class" and user.modifiers == ("abstract",) and user.bases == ("Base",)
    assert user.implements == ("I", "K", "T")  # interfaces, then composed traits
    assert symbols["App.Models.User.X"].kind == "constant"
    assert symbols["App.Models.User.name"].visibility == "private"
    assert symbols["App.Models.User.name"].signature.return_type == "?string"
    assert symbols["App.Models.User.count"].modifiers == ("static",)
    ctor = symbols["App.Models.User.__construct"]
    assert params(ctor) == [("id", "int", None, "positional"), ("rest", "string", None, "variadic")]
    promoted = symbols["App.Models.User.id"]
    assert (
        promoted.kind == "field"
        and promoted.modifiers == ("promoted",)
        and promoted.visibility == "private"
    )
    run = symbols["App.Models.User.run"]
    assert (
        run.visibility == "protected"
        and run.modifiers == ("abstract",)
        and run.signature.return_type == "void"
    )
    assert params(run) == [("a", "array", "[]", "positional"), ("b", "?int", "null", "positional")]
    assert symbols["App.Models.User.make"].modifiers == ("static",)
    assert symbols["App.Models.free"].kind == "function"
    suit = symbols["App.Models.Suit"]
    assert suit.kind == "enum" and suit.bases == ("string",)
    assert symbols["App.Models.Suit.H"].modifiers == ("case",)
    assert [
        (i.kind, i.module, [(n.name, n.alias) for n in i.names], i.alias) for i in result.imports
    ] == [
        ("static", "App\\Base", [], None),
        ("static", "App\\Contracts", [("A", None), ("B", "C")], None),
        ("static", "App\\helper", [], None),
        ("require", "x.php", [], None),
    ]
    assert [e.name for e in result.exports] == ["MAX", "I", "T", "User", "free", "Suit"]


def test_swift_types_extensions_protocols_members_and_imports():
    _, result = facts(SWIFT, "swift", "Sources/Foo.swift")
    assert result.status == "parsed" and result.grammar == "tree-sitter-swift"
    symbols = by_name(result)
    foo = symbols["Sources.Foo.Foo"]
    assert foo.kind == "class" and foo.decorators == ("objc",) and foo.exported
    assert foo.bases == ("Base", "Proto") and foo.implements == ("Equatable",)  # from the extension
    assert symbols["Sources.Foo.Foo.name"].modifiers == ("var",)
    assert symbols["Sources.Foo.Foo.id"].visibility == "private"
    assert symbols["Sources.Foo.Foo.id"].modifiers == ("let",)
    assert symbols["Sources.Foo.Foo.count"].modifiers == ("static", "var")
    assert symbols["Sources.Foo.Foo.init"].modifiers == ("constructor",)
    run = symbols["Sources.Foo.Foo.run"]
    assert run.modifiers == ("async", "throws") and run.signature.return_type == "Bool"
    assert params(run) == [
        ("x", "Int", None, "positional"),
        ("with y", "String", '"a"', "positional"),
        ("rest", "Int", None, "variadic"),
    ]
    assert symbols["Sources.Foo.Foo.make"].signature.return_type == "Foo"
    struct = symbols["Sources.Foo.P"]
    assert struct.kind == "struct" and struct.implements == ("Codable",)
    enum = symbols["Sources.Foo.E"]
    assert enum.kind == "enum" and enum.implements == ("Int",) and enum.visibility == "internal"
    assert {symbols["Sources.Foo.E.a"].modifiers, symbols["Sources.Foo.E.b"].modifiers} == {
        ("case",)
    }
    proto = symbols["Sources.Foo.Proto"]
    assert proto.kind == "interface" and proto.bases == ("Other",)
    assert symbols["Sources.Foo.Proto.m"].visibility == "public"
    ext_method = [s for s in result.symbols if s.qualified_name == "Sources.Foo.Foo.m"]
    assert [s.modifiers for s in ext_method] == [("extension",)]
    assert symbols["Sources.Foo.Act"].modifiers == ("actor",)
    assert (
        symbols["Sources.Foo.free"].exported and symbols["Sources.Foo.Alias"].kind == "type_alias"
    )
    assert symbols["Sources.Foo.GLOBAL"].kind == "constant"
    assert symbols["Sources.Foo.counter"].kind == "variable"
    assert [(i.kind, i.module) for i in result.imports] == [
        ("static", "Foundation"),
        ("static", "Swift.Int"),
    ]
    assert [e.name for e in result.exports] == ["Foo", "P", "free"]


CASES = [
    ("c", "src/sample.c", C),
    ("cpp", "src/sample.cpp", CPP),
    ("ruby", "lib/api.rb", RUBY),
    ("php", "src/Models/User.php", PHP),
    ("swift", "Sources/Foo.swift", SWIFT),
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
        ("c", "a.c", "int ok(void) { return 1; }\nint bad( {\n"),
        ("cpp", "a.cpp", "int ok() { return 1; }\nint bad( {\n"),
        ("ruby", "a.rb", "def ok; end\ndef bad(; end\n"),
        ("php", "a.php", "<?php\nfunction ok() {}\nfunction bad( {\n"),
        ("swift", "a.swift", "func ok() {}\nfunc bad( {\n"),
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


def test_every_registered_language_has_a_fact_extractor():
    assert {spec.name for spec in SPECS} == FACT_LANGUAGES
