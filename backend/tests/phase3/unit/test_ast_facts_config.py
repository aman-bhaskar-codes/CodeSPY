import hashlib
from uuid import uuid4

import pytest
from app.analyzers.facts import extract_facts
from app.analyzers.positions import SourceIndex
from app.contracts.structure import AnalysisBudget, SourceUnitV1

PYTHON = """import os
from flask import Flask
from django.conf import settings
app = Flask(__name__)
a = os.environ["A"]
b = os.environ.get("B", "super-secret-default")
c = os.getenv("C")
d = settings.DEBUG
e = app.config["E"]
f = app.config.get("F")
g = os.environ[dynamic_name]
"""

JAVASCRIPT = """const config = require("config");
const a = process.env.A;
const b = process.env["B"];
const c = import.meta.env.VITE_C;
Deno.env.get("D");
config.get("db.host");
const x = process.env[dyn];
"""

GO = """package a
import ("os"; "github.com/spf13/viper")
func f() { a := os.Getenv("A"); v, ok := os.LookupEnv("B"); c := viper.GetString("db.host") }
"""

JAVA = """package a;
@ConfigurationProperties(prefix = "app.db")
class Cfg {
  @Value("${app.name:hunter2}") String name;
  void f() { String s = System.getenv("A"); System.getProperty("b"); }
}
"""

CASES = [
    (
        "python",
        "a/cfg.py",
        PYTHON,
        [
            ("env_var", "A"),
            ("env_var", "B"),
            ("env_var", "C"),
            ("config_key", "DEBUG"),
            ("config_key", "E"),
            ("config_key", "F"),
        ],
    ),
    (
        "javascript",
        "a/cfg.js",
        JAVASCRIPT,
        [
            ("env_var", "A"),
            ("env_var", "B"),
            ("env_var", "VITE_C"),
            ("env_var", "D"),
            ("config_key", "db.host"),
        ],
    ),
    ("go", "a/cfg.go", GO, [("env_var", "A"), ("env_var", "B"), ("config_key", "db.host")]),
    (
        "java",
        "a/Cfg.java",
        JAVA,
        [
            ("config_key", "app.db"),
            ("config_key", "app.name"),
            ("env_var", "A"),
            ("config_key", "b"),
        ],
    ),
    (
        "rust",
        "a/cfg.rs",
        'fn f() { let a = std::env::var("A"); let b = env!("B"); let c = option_env!("C");'
        ' let d = env::var_os("D"); }\n',
        [("env_var", "A"), ("env_var", "B"), ("env_var", "C"), ("env_var", "D")],
    ),
    (
        "ruby",
        "a/cfg.rb",
        "a = ENV['A']\nb = ENV.fetch('B', 'hunter2')\n",
        [("env_var", "A"), ("env_var", "B")],
    ),
    (
        "php",
        "a/cfg.php",
        "<?php\n$a = getenv('A'); $b = $_ENV['B']; $c = env('C'); $d = config('app.name');"
        " $e = $_SERVER['F'];\n",
        [("env_var", "A"), ("env_var", "B"), ("env_var", "C"), ("config_key", "app.name")],
    ),
    (
        "swift",
        "a/cfg.swift",
        'let a = ProcessInfo.processInfo.environment["A"]\n',
        [("env_var", "A")],
    ),
    ("c", "a/cfg.c", 'void f() { char *a = getenv("A"); }\n', [("env_var", "A")]),
    ("cpp", "a/cfg.cpp", 'void f() { auto a = std::getenv("A"); }\n', [("env_var", "A")]),
]


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


@pytest.mark.parametrize(("language", "path", "source", "expected"), CASES)
def test_environment_and_config_reads_are_recorded_by_name(language, path, source, expected):
    _, result = facts(source, language, path)
    assert [(c.kind, c.name) for c in result.config] == expected


@pytest.mark.parametrize(("language", "path", "source", "expected"), CASES)
def test_only_names_are_stored_never_defaults_or_values(language, path, source, expected):
    _, result = facts(source, language, path)
    stored = result.model_dump_json()
    for fallback in ("super-secret-default", "hunter2"):
        assert fallback not in stored


def test_spring_placeholder_defaults_are_masked_in_annotations():
    _, result = facts(JAVA, "java", "a/Cfg.java")
    (field,) = [s for s in result.symbols if s.name == "name"]
    assert field.decorators == ('Value("${app.name:…}")',)


def test_computed_names_and_request_data_are_not_config():
    _, result = facts(PYTHON, "python", "a/cfg.py")
    assert "dynamic_name" not in {c.name for c in result.config}
    _, php = facts("<?php\n$e = $_SERVER['REQUEST_METHOD'];\n", "php", "a.php")
    assert php.config == ()


def test_framework_specific_reads_require_the_framework_import():
    source = 'a = app.config["E"]\nb = settings.DEBUG\n'
    _, result = facts(source, "python", "a.py")  # neither flask nor django is imported
    assert result.config == ()
    _, js = facts('config.get("db.host");\n', "javascript", "a.js")  # config not imported
    assert js.config == ()
    _, go = facts('package a\nfunc f() { viper.GetString("k") }\n', "go", "a.go")
    assert go.config == ()


def test_files_without_a_marker_are_not_reparsed(monkeypatch):
    import app.analyzers.facts.config as config

    def explode(*args, **kwargs):
        raise AssertionError("parsed a file with no configuration marker")

    monkeypatch.setattr(config, "parse_bytes", explode)
    _, result = facts("def f():\n    return 1\n", "python", "a.py")
    assert result.config == ()


@pytest.mark.parametrize(("language", "path", "source", "expected"), CASES)
def test_every_config_fact_cites_original_bytes_and_is_deterministic(
    language, path, source, expected
):
    unit, result = facts(source, language, path)
    index = SourceIndex(unit.data)
    for item in result.config:
        index.validate_range(item.range)
    again = extract_facts(unit, AnalysisBudget(max_syntax_nodes=200000))
    assert result.facts_sha256() == again.facts_sha256()


def test_config_facts_are_capped_per_file():
    body = "".join(f'x{i} = os.getenv("V{i}")\n' for i in range(1200))
    _, result = facts("import os\n" + body, "python", "big.py")
    assert len(result.config) == 1024
