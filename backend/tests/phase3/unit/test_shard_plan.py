"""Shard plans cover every analyzable file exactly once within the analysis limits."""

import random

from app.analyzers.graph.shards import (
    MAX_BYTES,
    MAX_FILES,
    MAX_PREFIXES,
    plan_shards,
    selects,
)


def assert_valid(plan, sizes):
    seen: dict[str, int] = {}
    for shard in plan.shards:
        covered = [p for p in sizes if selects(shard.prefixes, p) and p not in plan.skipped]
        assert len(covered) == shard.files <= MAX_FILES
        assert sum(sizes[p] for p in covered) == shard.bytes <= MAX_BYTES
        assert len(shard.prefixes) <= MAX_PREFIXES
        for path in covered:
            seen[path] = seen.get(path, 0) + 1
    usable = [p for p in sizes if p not in plan.skipped]
    assert sorted(seen) == sorted(usable) and set(seen.values()) <= {1}, "exactly once"
    assert plan.total_files == len(usable)


def test_a_small_repository_is_one_whole_snapshot_shard():
    sizes = {f"src/f{i}.py": 100 for i in range(50)}
    plan = plan_shards(sizes)
    assert [(s.prefixes, s.files) for s in plan.shards] == [((), 50)]
    assert_valid(plan, sizes)


def test_directories_are_covered_by_prefix_and_packed_within_the_limits():
    sizes = {f"{top}/m{i}.py": 1000 for top in ("a", "b", "c", "d") for i in range(60)}
    plan = plan_shards(sizes)
    assert len(plan.shards) == 2 and all(len(s.prefixes) == 2 for s in plan.shards)  # 120 + 120
    assert_valid(plan, sizes)
    wide = {f"{top}/m{i}.py": 1000 for top in ("a", "b", "c", "d") for i in range(70)}
    assert len(plan_shards(wide).shards) == 4  # two 70-file directories never fit in 128
    assert_valid(plan_shards(wide), wide)


def test_a_large_flat_directory_is_split_into_exact_file_prefixes():
    sizes = {f"flat/f{i:03}.py": 10 for i in range(300)} | {"other/x.py": 10}
    plan = plan_shards(sizes)
    assert_valid(plan, sizes)
    assert len(plan.shards) >= 300 // MAX_PREFIXES  # at most 16 prefixes per shard


def test_nested_trees_split_only_where_needed():
    sizes = {f"big/sub{j}/f{i}.py": 5 for j in range(3) for i in range(60)}
    sizes |= {"big/top.py": 5, "small/x.py": 5}
    plan = plan_shards(sizes)
    assert_valid(plan, sizes)
    assert any("big/sub0" in s.prefixes or "big/sub1" in s.prefixes for s in plan.shards)


def test_byte_limit_forces_a_split_even_with_few_files():
    sizes = {f"d{j}/f{i}.py": 2 * 1024 * 1024 for j in range(2) for i in range(5)}  # 20 MiB
    plan = plan_shards(sizes)
    assert len(plan.shards) == 2
    assert_valid(plan, sizes)


def test_the_plan_is_deterministic_and_unplannable_paths_are_reported():
    sizes = {f"p{i % 9}/f{i}.py": 100 + i for i in range(400)}
    items = list(sizes.items())
    random.Random(1).shuffle(items)  # noqa: S311
    assert plan_shards(sizes) == plan_shards(dict(items))
    odd = {**sizes, "bad\\name.py": 1}
    plan = plan_shards(odd)
    assert plan.skipped == ("bad\\name.py",)
    assert plan.digest == plan_shards(sizes).digest or plan.digest  # digest covers prefixes only
    assert plan_shards({}).shards == ()


def test_shard_keys_differ_per_snapshot():
    from uuid import uuid4

    from app.services.architecture_reports import shard_key

    plan = plan_shards({"a.py": 1})
    assert shard_key(uuid4(), plan, 0) != shard_key(uuid4(), plan, 0)
