"""The host-side workspace never follows a link or special file a hostile process plants in it."""

import os
import stat
from pathlib import Path

import pytest
from app.adapters.sandbox.hosttree import HostTree
from app.ports.sandbox import MAX_FILE_BYTES, NotATextFile, PathRejected, SandboxLimit


@pytest.fixture
def tree(tmp_path: Path) -> HostTree:
    return HostTree(tmp_path / "ws")


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A file outside the workspace that must never be read or changed."""
    secret = tmp_path / "host-secret.txt"
    secret.write_text("host secret")
    return secret


def test_write_read_and_hash_round_trip(tree):
    tree.write("a/b/c.txt", b"hello\n")
    assert tree.read("a/b/c.txt") == b"hello\n"
    assert tree.kind("a/b/c.txt") == "file" and tree.kind("a/b") == "dir" and tree.kind("x") is None
    facts = tree.facts()
    assert list(facts) == ["a/b/c.txt"] and facts["a/b/c.txt"].size == 6
    assert tree.read("missing.txt") is None


def test_a_symlinked_file_is_neither_read_nor_replaced_through_the_link(tree, outside):
    os.symlink(outside, tree.root / "link.txt")
    assert tree.kind("link.txt") == "other"
    with pytest.raises(NotATextFile):
        tree.read("link.txt")
    tree.write("link.txt", b"new")  # replaces the link itself, never writes through it
    assert outside.read_text() == "host secret"
    assert (tree.root / "link.txt").is_file() and not (tree.root / "link.txt").is_symlink()


def test_a_symlinked_directory_is_not_traversed_for_reads_writes_or_moves(tree, outside):
    os.symlink(outside.parent, tree.root / "out")
    assert tree.read("out/host-secret.txt") is None
    assert tree.kind("out/host-secret.txt") is None and not tree.dir_exists("out")
    with pytest.raises(PathRejected):
        tree.write("out/planted.txt", b"x")
    assert not (outside.parent / "planted.txt").exists()
    tree.write("real.txt", b"r")
    with pytest.raises(PathRejected):
        tree.move("real.txt", "out/moved.txt")
    assert not (outside.parent / "moved.txt").exists()
    with pytest.raises(PathRejected):
        tree.delete("out/host-secret.txt")
    assert outside.exists()


def test_a_link_in_the_middle_of_a_created_path_is_refused(tree, outside):
    (tree.root / "src").mkdir()
    os.symlink(outside.parent, tree.root / "src" / "deep")
    with pytest.raises(PathRejected):
        tree.write("src/deep/new.txt", b"x")
    assert not (outside.parent / "new.txt").exists()


def test_links_and_special_files_are_not_files(tree, outside):
    tree.write("real.txt", b"r")
    os.symlink(outside, tree.root / "l.txt")
    os.symlink(outside.parent, tree.root / "d")
    os.mkfifo(tree.root / "pipe")
    assert list(tree.facts()) == ["real.txt"]  # nothing outside is hashed, a FIFO is never opened
    with pytest.raises(NotATextFile):
        tree.read("pipe")
    assert tree.total_bytes() == 1


def test_a_directory_cannot_be_overwritten_or_read_as_a_file(tree):
    (tree.root / "dir").mkdir()
    with pytest.raises(PathRejected):
        tree.write("dir", b"x")
    with pytest.raises(NotATextFile):
        tree.read("dir")


def test_oversized_files_are_refused_on_read(tree):
    big = tree.root / "big.bin"
    with big.open("wb") as f:
        f.truncate(MAX_FILE_BYTES + 1)
    with pytest.raises(SandboxLimit):
        tree.read("big.bin")


def test_git_and_temporary_names_are_invisible(tree):
    tree.write("a.txt", b"a")
    (tree.root / ".git").mkdir()
    (tree.root / ".git" / "config").write_text("[remote]")
    (tree.root / "sub").mkdir()
    (tree.root / "sub" / ".git").mkdir()
    (tree.root / "sub" / ".git" / "HEAD").write_text("ref")
    (tree.root / ".cs-0123456789abcdef.tmp").write_text("half written")
    assert list(tree.facts()) == ["a.txt"]


def test_overwrite_keeps_permission_bits_and_leaves_no_temp_file(tree):
    tree.write("run.sh", b"#!/bin/sh\n")
    (tree.root / "run.sh").chmod(0o755)
    tree.write("run.sh", b"#!/bin/sh\necho hi\n")
    assert stat.S_IMODE((tree.root / "run.sh").stat().st_mode) == 0o755
    assert sorted(p.name for p in tree.root.iterdir()) == ["run.sh"]


def test_move_and_delete_and_clear(tree):
    tree.write("a/one.txt", b"1")
    tree.write("two.txt", b"2")
    tree.move("a/one.txt", "b/c/one.txt")
    assert tree.read("b/c/one.txt") == b"1" and tree.read("a/one.txt") is None
    tree.delete("two.txt")
    assert sorted(tree.facts()) == ["b/c/one.txt"]
    os.symlink("/", tree.root / "rootlink")
    tree.clear()  # removes the link itself and never follows it
    assert list(tree.root.iterdir()) == [] and Path("/").exists()


def test_hash_cache_tracks_changes_made_behind_its_back(tree):
    tree.write("f.txt", b"one")
    first = tree.facts()["f.txt"].sha256
    path = tree.root / "f.txt"
    path.write_bytes(b"two!")  # a command in the sandbox rewrote the file
    assert tree.facts()["f.txt"].sha256 != first and tree.facts()["f.txt"].size == 4
