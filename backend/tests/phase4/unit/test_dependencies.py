"""Exact public-registry identities; unknown dependency sources stay unknown."""

import json

import pytest
from app.scanners.dependencies import LockfileError, parse_package_lock, parse_uv_lock


def lock(packages=None, version=3):
    return json.dumps({"lockfileVersion": version, "packages": packages or {}}).encode()


def test_npm_nested_versions_are_not_collapsed_and_alias_uses_real_identity():
    result = parse_package_lock(
        lock(
            {
                "": {"name": "app"},
                "node_modules/a": {
                    "version": "1.2.3",
                    "resolved": "https://registry.npmjs.org/a/-/a-1.2.3.tgz",
                },
                "node_modules/x/node_modules/a": {
                    "version": "1.1.0",
                    "resolved": "https://registry.npmjs.org/a/-/a-1.1.0.tgz",
                },
                "node_modules/alias": {
                    "name": "@scope/real",
                    "version": "2.0.0",
                    "resolved": "https://registry.npmjs.org/@scope/real/-/real-2.0.0.tgz",
                },
            }
        )
    )
    assert [(p.name, p.version) for p in result.packages] == [
        ("a", "1.2.3"),
        ("@scope/real", "2.0.0"),
        ("a", "1.1.0"),
    ]
    assert result.complete


@pytest.mark.parametrize(
    "entry",
    [
        {"version": "1.0.0", "resolved": "git+https://github.com/x/y"},
        {"version": "1.0.0", "resolved": "https://private.example/a.tgz"},
        {"version": "1.0.0", "link": True},
        {"version": "file:../local"},
        {"version": "^1.0.0", "resolved": "https://registry.npmjs.org/a/-/a.tgz"},
        {"version": "1.0.0", "resolved": "https://registry.npmjs.org.evil/a"},
    ],
)
def test_npm_nonregistry_or_inexact_dependencies_are_unresolved(entry):
    result = parse_package_lock(lock({"node_modules/a": entry}))
    assert result.packages == ()
    assert result.unresolved == ("node_modules/a",)
    assert not result.complete


def test_uv_preserves_marker_variants_and_normalizes_public_pypi_names():
    result = parse_uv_lock(b"""version = 1
revision = 3
[[package]]
name = "My_Package"
version = "1.2.3"
source = { registry = "https://pypi.org/simple" }
resolution-markers = ["sys_platform == 'linux'"]
[[package]]
name = "my-package"
version = "1.2.4"
source = { registry = "https://pypi.org/simple" }
""")
    assert [(p.name, p.version) for p in result.packages] == [
        ("my-package", "1.2.3"),
        ("my-package", "1.2.4"),
    ]
    assert result.packages[0].markers == ("sys_platform == 'linux'",)
    assert result.complete


def test_uv_local_root_is_excluded_but_git_and_private_registry_remain_unresolved():
    result = parse_uv_lock(b"""version = 1
revision = 3
[[package]]
name = "root"
version = "0.1.0"
source = { virtual = "." }
[[package]]
name = "one"
version = "1.0"
source = { git = "https://github.com/x/y" }
[[package]]
name = "two"
version = "1.0"
source = { registry = "https://private.example/simple" }
""")
    assert result.packages == ()
    assert result.unresolved == ("package[1]", "package[2]")
    assert not result.complete


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"[]",
        lock(version=2),
        b"x" * 262145,
        b'{"lockfileVersion":3,"lockfileVersion":3,"packages":{}}',
    ],
    ids=["syntax", "shape", "version", "capacity", "duplicate-key"],
)
def test_unsupported_or_corrupt_npm_lock_is_explicit(raw):
    with pytest.raises(LockfileError):
        parse_package_lock(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b"version=2\nrevision=3",
        b"version=1\nrevision=4",
        b"version=1\nrevision=3\npackage=3",
        b"[invalid",
    ],
    ids=["version", "revision", "shape", "syntax"],
)
def test_unsupported_or_corrupt_uv_lock_is_explicit(raw):
    with pytest.raises(LockfileError):
        parse_uv_lock(raw)


def test_uv_without_an_explicit_package_inventory_is_not_an_empty_inventory():
    with pytest.raises(LockfileError):
        parse_uv_lock(b"version=1\nrevision=3\n")


@pytest.mark.parametrize(
    "version", ["1.2.3-01", "1.2.3-alpha..1", "1.2.3+build..x", "1.2.3-.", "1.2.3+.."]
)
def test_malformed_semver_cannot_become_an_exact_public_identity(version):
    result = parse_package_lock(
        lock(
            {
                "node_modules/a": {
                    "version": version,
                    "resolved": "https://registry.npmjs.org/a/-/a.tgz",
                }
            }
        )
    )
    assert result.packages == ()
    assert result.unresolved == ("node_modules/a",)


@pytest.mark.parametrize("version", ["1.2.3-alpha.0+001", "1.2.3-0", "1.2.3-x.--"])
def test_valid_prerelease_and_build_metadata_remain_exact_identities(version):
    result = parse_package_lock(
        lock(
            {
                "node_modules/a": {
                    "version": version,
                    "resolved": "https://registry.npmjs.org/a/-/a.tgz",
                }
            }
        )
    )
    assert result.packages[0].version == version
    assert result.complete
