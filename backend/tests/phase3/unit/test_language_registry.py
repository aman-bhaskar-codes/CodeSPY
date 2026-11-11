import tomllib
from pathlib import Path
from typing import get_args

import pytest
from app.admission.zip_inventory import EXTENDED_LANGUAGES, LANGUAGES
from app.analyzers.registry import (
    BY_NAME,
    FACT_DIALECTS,
    SPECS,
    STRUCTURAL_DIALECTS,
    parse_bytes,
)
from app.contracts.structure import Language

ROOT = Path(__file__).resolve().parents[4]
SAMPLES = {
    "python": b"def f(): pass\n",
    "javascript": b"function f() {}\n",
    "jsx": b"const v = <div/>;\n",
    "typescript": b"function f(x: number) {}\n",
    "tsx": b"const v = <div/>;\n",
    "go": b"package main\nfunc f() {}\n",
    "rust": b"fn f() {}\n",
    "java": b"class A { void f() {} }\n",
    "c": b"int f(void) { return 0; }\n",
    "cpp": b"class A { void f(); };\n",
    "ruby": b"def f; end\n",
    "php": b"<?php function f() {}\n",
    "swift": b"func f() {}\n",
}


def test_every_language_literal_has_exactly_one_spec():
    assert {s.name for s in SPECS} == set(get_args(Language)) == set(BY_NAME)
    assert len(SPECS) == len(BY_NAME)


def test_structural_dialects_are_the_original_five_and_unchanged():
    assert STRUCTURAL_DIALECTS == {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "jsx",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
    }


def test_fact_dialects_extend_structural_without_overlap_or_ambiguity():
    assert set(STRUCTURAL_DIALECTS.items()) <= set(FACT_DIALECTS.items())
    extensions = [ext for spec in SPECS for ext in spec.extensions]
    assert len(extensions) == len(set(extensions))  # no suffix claimed twice
    assert all(ext.startswith(".") and ext == ext.casefold() for ext in extensions)


def test_admission_extension_table_matches_the_analyzer_registry():
    new = {s.name: s for s in SPECS if not s.structural}
    for ext, hint in EXTENDED_LANGUAGES.items():
        if ext not in LANGUAGES:
            assert FACT_DIALECTS[ext] == hint and BY_NAME[hint] is new[hint]
    admitted_new = {ext for ext in EXTENDED_LANGUAGES if ext not in LANGUAGES}
    assert admitted_new == {ext for s in new.values() for ext in s.extensions}


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_every_registered_grammar_loads_and_parses(name):
    tree = parse_bytes(name, SAMPLES[name])
    assert not tree.root_node.has_error and tree.root_node.end_byte == len(SAMPLES[name])


def test_samples_cover_every_language():
    assert set(SAMPLES) == set(BY_NAME)


def test_registry_pins_are_locked_in_the_workspace_lockfile():
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {p["name"] for p in lock["package"]}
    assert {s.pin for s in SPECS} <= locked
    pyproject = (ROOT / "backend/pyproject.toml").read_text()
    for pin in {s.pin for s in SPECS}:
        assert f'"{pin}==' in pyproject  # exact pins only
