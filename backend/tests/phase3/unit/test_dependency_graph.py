# ruff: noqa: E501
"""Import resolution per language over real extracted facts."""

import hashlib
import random
from pathlib import Path
from uuid import uuid4

from app.analyzers.facts import extract_facts
from app.analyzers.graph import FileEntry, build_graph
from app.analyzers.registry import FACT_DIALECTS
from app.contracts.structure import AnalysisBudget, SourceUnitV1


def entries(files: dict[str, str]) -> list[FileEntry]:
    result = []
    for path, text in files.items():
        language = FACT_DIALECTS.get(Path(path).suffix.casefold(), "manifest")
        data = text.encode()
        unit = SourceUnitV1(
            file_id=uuid4(),
            path=path,
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
            language=language,
        )
        result.append(
            FileEntry(unit.file_id, path, language, extract_facts(unit, AnalysisBudget()))
        )
    return result


def split(files: dict[str, str]):
    items = entries(files)
    return [e for e in items if e.language != "manifest"], [
        e for e in items if e.language == "manifest"
    ]


def summarize(files: dict[str, str], inventory=()):
    sources, manifests = split(files)
    graph = build_graph(sources, inventory, manifests)
    out: dict[str, list[tuple]] = {}
    for e in graph.edges:
        source = e.source.removeprefix("file:")
        shown = e.target if e.resolution in {"resolved", "external"} else e.candidates or None
        out.setdefault(source, []).append((e.module, e.resolution, shown))
    return graph, out


def test_python_resolves_relative_absolute_and_submodule_imports_and_classifies_the_rest():
    _, out = summarize(
        {
            "app/main.py": (
                "import os\nimport requests\nfrom app import util\nfrom . import helpers\n"
                "from .models import User\nimport app.core.engine as engine\n"
                "from missing_pkg_dir import thing\nfrom app.core import *\n"
            ),
            "app/__init__.py": "",
            "app/util.py": "",
            "app/helpers.py": "",
            "app/models.py": "",
            "app/core/__init__.py": "",
            "app/core/engine.py": "",
        },
        inventory=["missing_pkg_dir/x.py"],  # a Python file that was not analyzed,
    )
    got = {(m, r, t) for m, r, t in out["app/main.py"]}
    assert ("os", "external", "ext:python:os") in got
    assert ("requests", "external", "ext:python:requests") in got
    assert ("app", "resolved", "file:app/__init__.py") in got
    assert ("app", "resolved", "file:app/util.py") in got  # the submodule named in from-import
    assert ("", "resolved", "file:app/__init__.py") in got
    assert ("", "resolved", "file:app/helpers.py") in got
    assert ("models", "resolved", "file:app/models.py") in got
    assert ("app.core.engine", "resolved", "file:app/core/engine.py") in got
    assert ("app.core", "resolved", "file:app/core/__init__.py") in got
    # a name that exists in the repository inventory but cannot be located is unknown, not external
    assert ("missing_pkg_dir", "unresolved", None) in got


def test_python_equally_close_matches_are_ambiguous_and_closer_ones_win():
    _, out = summarize(
        {
            "svc_a/main.py": "import util\n",
            "svc_a/util.py": "",
            "svc_b/util.py": "",
            "x/other.py": "import util\n",
        }
    )
    assert out["svc_a/main.py"] == [("util", "resolved", "file:svc_a/util.py")]
    assert out["x/other.py"][0][1] == "ambiguous"
    assert set(out["x/other.py"][0][2]) == {"file:svc_a/util.py", "file:svc_b/util.py"}


def test_javascript_and_typescript_resolution():
    _, out = summarize(
        {
            "src/a.ts": (
                "import x from './x';\nimport {y} from '../lib/y.js';\nimport z from './dir';\n"
                "import l from 'lodash';\nimport s from '@scope/pkg/sub';\nimport a from '@/alias';\n"
                "import f from 'node:fs';\nimport m from './missing';\nexport * from './x';\n"
                "const r = require('./x');\n"
            ),
            "src/x.ts": "",
            "lib/y.ts": "",
            "src/dir/index.tsx": "",
        }
    )
    got = {(m, r, t) for m, r, t in out["src/a.ts"]}
    assert ("./x", "resolved", "file:src/x.ts") in got
    assert ("../lib/y.js", "resolved", "file:lib/y.ts") in got  # .js specifier maps to .ts
    assert ("./dir", "resolved", "file:src/dir/index.tsx") in got
    assert ("lodash", "external", "ext:js:lodash") in got
    assert ("@scope/pkg/sub", "external", "ext:js:@scope/pkg") in got
    assert ("node:fs", "external", "ext:js:node:fs") in got
    assert ("@/alias", "unresolved", None) in got  # aliases need build configuration
    assert ("./missing", "unresolved", None) in got
    assert sum(1 for m, r, _ in out["src/a.ts"] if m == "./x") == 3  # import, reexport, require


def test_javascript_relative_imports_never_escape_the_repository():
    _, out = summarize({"a.js": "import x from '../../outside';\n"})
    assert out["a.js"] == [("../../outside", "unresolved", None)]


def test_go_resolves_packages_by_suffix_and_infers_the_module_prefix():
    graph, out = summarize(
        {
            "main.go": (
                'package main\nimport (\n "fmt"\n "github.com/org/repo/internal/util"\n'
                ' "github.com/org/repo/binding"\n "github.com/other/lib/x"\n)\n'
            ),
            "internal/util/u.go": "package util\n",
            "binding/b.go": "package binding\n",
            "cmd/run.go": 'package cmd\nimport "github.com/org/repo/binding"\n',
        }
    )
    got = {(m, r, t) for m, r, t in out["main.go"]}
    assert ("fmt", "external", "ext:go:fmt") in got
    assert ("github.com/org/repo/internal/util", "resolved", "pkg:internal/util") in got
    # `repo/binding` is not a path in the checkout; the module prefix learned from the other
    # import places it at the repository root
    assert ("github.com/org/repo/binding", "resolved", "pkg:binding") in got
    assert ("github.com/other/lib/x", "external", "ext:go:github.com/other/lib") in got
    assert any(n.kind == "package" and n.id == "pkg:internal/util" for n in graph.nodes)


def test_java_classes_wildcards_static_imports_and_external_packages():
    _, out = summarize(
        {
            "src/com/ex/A.java": (
                "package com.ex;\nimport com.ex.util.Helper;\nimport java.util.List;\n"
                "import static com.ex.Consts.MAX;\nimport org.spring.*;\nimport com.ex.util.*;\n"
            ),
            "src/com/ex/util/Helper.java": "package com.ex.util;\n",
            "src/com/ex/Consts.java": "package com.ex;\n",
        }
    )
    got = {(m, r, t) for m, r, t in out["src/com/ex/A.java"]}
    assert ("com.ex.util.Helper", "resolved", "file:src/com/ex/util/Helper.java") in got
    assert ("java.util.List", "external", "ext:java:java") in got
    assert ("com.ex.Consts", "resolved", "file:src/com/ex/Consts.java") in got
    assert ("org.spring", "external", "ext:java:org.spring") in got
    assert ("com.ex.util", "resolved", "pkg:src/com/ex/util") in got


def test_rust_crate_super_and_external_paths():
    _, out = summarize(
        {
            "src/lib.rs": "",
            "src/x.rs": "",
            "src/a/mod.rs": "",
            "src/a/b.rs": (
                "use crate::x::Y;\nuse super::helper;\nuse std::io::Read;\nuse serde::Serialize;\n"
                "use crate::nowhere::Z;\n"
            ),
        }
    )
    got = {(m, r, t) for m, r, t in out["src/a/b.rs"]}
    assert ("crate::x::Y", "resolved", "file:src/x.rs") in got
    assert ("super::helper", "resolved", "file:src/a/mod.rs") in got
    assert ("std::io::Read", "external", "ext:rust:std") in got
    assert ("serde::Serialize", "external", "ext:rust:serde") in got
    assert ("crate::nowhere::Z", "resolved", "file:src/lib.rs") in got  # deepest module found


def test_c_family_includes():
    _, out = summarize(
        {
            "src/a.c": '#include <stdio.h>\n#include "local.h"\n#include "sub/dir.h"\n#include "gone.h"\n'
            "#include <proj/api.h>\n",
            "src/local.h": "",
            "src/sub/dir.h": "",
            "include/proj/api.h": "",
        }
    )
    got = {(m, r, t) for m, r, t in out["src/a.c"]}
    assert ("stdio.h", "external", "ext:c:stdio.h") in got
    assert ("local.h", "resolved", "file:src/local.h") in got
    assert ("sub/dir.h", "resolved", "file:src/sub/dir.h") in got
    assert ("gone.h", "unresolved", None) in got
    assert (
        "proj/api.h",
        "resolved",
        "file:include/proj/api.h",
    ) in got  # angle include, project root


def test_ruby_php_and_swift():
    _, out = summarize(
        {
            "lib/a.rb": "require 'json'\nrequire_relative 'b'\nrequire 'my/lib'\n",
            "lib/b.rb": "",
            "lib/my/lib.rb": "",
            "src/Http/Ctl.php": (
                "<?php\nnamespace App\\Http;\nuse App\\Models\\User;\n"
                "use Illuminate\\Support\\{Str, Arr};\nrequire_once 'x.php';\n"
            ),
            "src/Models/User.php": "<?php\n",
            "src/Http/x.php": "<?php\n",
            "Sources/App/a.swift": "import Foundation\nimport MyCore\n",
            "Sources/MyCore/core.swift": "",
        }
    )
    ruby = {(m, r, t) for m, r, t in out["lib/a.rb"]}
    assert ("json", "external", "ext:ruby:json") in ruby
    assert ("b", "resolved", "file:lib/b.rb") in ruby
    assert ("my/lib", "resolved", "file:lib/my/lib.rb") in ruby
    php = {(m, r, t) for m, r, t in out["src/Http/Ctl.php"]}
    assert ("App\\Models\\User", "resolved", "file:src/Models/User.php") in php
    assert ("Illuminate\\Support", "external", "ext:php:Illuminate") in php
    assert ("x.php", "resolved", "file:src/Http/x.php") in php
    swift = {(m, r, t) for m, r, t in out["Sources/App/a.swift"]}
    assert ("Foundation", "external", "ext:swift:Foundation") in swift
    assert ("MyCore", "resolved", "pkg:Sources/MyCore") in swift


def test_the_graph_is_closed_deterministic_and_independent_of_input_order():
    files = {
        "a/x.py": "from a import y\nimport os\n",
        "a/y.py": "from . import x\n",
        "a/__init__.py": "",
        "web/app.ts": "import {a} from './b';\n",
        "web/b.ts": "import {a} from './app';\n",
    }
    graph, _ = summarize(files)  # constructing validates that every endpoint is a node
    shuffled = list(files.items())
    random.Random(7).shuffle(shuffled)  # noqa: S311
    again = build_graph(entries(dict(shuffled)))
    assert [(e.source, e.target, e.resolution, e.module) for e in again.edges] == [
        (e.source, e.target, e.resolution, e.module) for e in graph.edges
    ]
    assert [n.id for n in again.nodes] == [n.id for n in graph.nodes]
    assert graph.counts()["resolved"] >= 4


def test_go_module_root_package_and_java_framework_prefixes_found_by_calibration():
    # The corpus run showed these two cases wrong: the module's root package is its own import
    # path, and a framework shares a leading directory segment with the application's packages.
    _, go = summarize(
        {
            "gin.go": 'package gin\nimport "github.com/org/repo/internal/x"\n',
            "internal/x/x.go": "package x\n",
            "t/t.go": 'package t\nimport "github.com/org/repo"\n',
        }
    )
    assert ("github.com/org/repo", "resolved", "pkg:.") in go["t/t.go"]
    _, java = summarize(
        {
            "src/org/springframework/samples/app/A.java": (
                "package org.springframework.samples.app;\n"
                "import org.springframework.beans.factory.annotation.Autowired;\n"
                "import org.springframework.samples.app.B;\n"
                "import org.springframework.samples.app.Missing;\n"
            ),
            "src/org/springframework/samples/app/B.java": "package org.springframework.samples.app;\n",
        }
    )
    got = {(m, r) for m, r, _ in java["src/org/springframework/samples/app/A.java"]}
    assert ("org.springframework.beans.factory.annotation.Autowired", "external") in got
    assert ("org.springframework.samples.app.B", "resolved") in got
    assert ("org.springframework.samples.app.Missing", "unresolved") in got  # internal, absent file


def test_a_python_fixture_named_like_a_library_does_not_capture_every_importer():
    # Found on the Flask corpus: tests/.../flask.py would otherwise win for every nearby importer.
    _, out = summarize(
        {
            "src/flask/__init__.py": "",
            "tests/t/test_a.py": "import flask\n",
            "tests/t/deep/flask.py": "",
            "tests/t/test_b.py": "import deep\n",
            "tests/t/deep/__init__.py": "",
        }
    )
    kind = out["tests/t/test_a.py"][0]
    assert kind[1] == "ambiguous" and set(kind[2]) == {
        "file:src/flask/__init__.py",
        "file:tests/t/deep/flask.py",
    }
    assert out["tests/t/test_b.py"] == [("deep", "resolved", "file:tests/t/deep/__init__.py")]


def test_rust_mod_declarations_are_file_dependencies():
    graph = build_graph(
        entries(
            {
                "src/lib.rs": "mod util;\nmod net;\n",
                "src/util.rs": "pub fn f() {}\n",
                "src/net/mod.rs": "pub fn g() {}\n",
            }
        ),
        inventory=("src/lib.rs", "src/util.rs", "src/net/mod.rs"),
    )
    targets = {(e.source, e.target) for e in graph.edges if e.resolution == "resolved"}
    assert ("file:src/lib.rs", "file:src/util.rs") in targets
    assert ("file:src/lib.rs", "file:src/net/mod.rs") in targets


def test_rust_integration_tests_are_their_own_crate_root():
    graph = build_graph(
        entries(
            {
                "tests/test_a.rs": "mod common;\n",
                "tests/common/mod.rs": "pub fn f() {}\n",
            }
        ),
        inventory=("tests/test_a.rs", "tests/common/mod.rs"),
    )
    assert any(
        e.resolution == "resolved" and e.target == "file:tests/common/mod.rs" for e in graph.edges
    )


def resolutions(files, inventory=()):
    sources, manifests = split(files)
    graph = build_graph(sources, inventory, manifests)
    return {(e.module, e.resolution, e.target) for e in graph.edges}


def test_go_mod_module_path_resolves_exactly_even_without_other_evidence():
    got = resolutions(
        {
            "go.mod": "module example.com/acme/app\n",
            "cmd/main.go": 'package main\nimport (\n"example.com/acme/app/internal/db"\n"example.com/acme/app/missing"\n"github.com/x/y"\n)\n',
            "internal/db/db.go": "package db\n",
        }
    )
    assert ("example.com/acme/app/internal/db", "resolved", "pkg:internal/db") in got
    assert ("example.com/acme/app/missing", "unresolved", None) in got
    assert ("github.com/x/y", "external", "ext:go:github.com/x/y") in got


def test_npm_workspace_package_names_resolve_to_their_directory():
    got = resolutions(
        {
            "package.json": '{"name": "root", "workspaces": ["packages/*"]}',
            "packages/ui/package.json": '{"name": "@acme/ui"}',
            "packages/ui/index.ts": "export const a = 1;\n",
            "packages/app/src/main.ts": 'import { a } from "@acme/ui/button";\nimport x from "react";\n',
        }
    )
    assert ("@acme/ui/button", "resolved", "pkg:packages/ui") in got
    assert ("react", "external", "ext:js:react") in got


def test_cargo_crate_names_resolve_across_a_workspace():
    got = resolutions(
        {
            "crates/core/Cargo.toml": '[package]\nname = "my-core"\n',
            "crates/core/src/lib.rs": "pub mod util;\n",
            "crates/core/src/util.rs": "pub fn f() {}\n",
            "crates/cli/Cargo.toml": '[package]\nname = "cli"\n',
            "crates/cli/src/main.rs": "use my_core::util::f;\nuse serde::Serialize;\n",
        }
    )
    assert ("my_core::util::f", "resolved", "file:crates/core/src/util.rs") in got
    assert ("serde::Serialize", "external", "ext:rust:serde") in got
