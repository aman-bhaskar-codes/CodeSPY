"""Architecture rules: forbidden, layers and independence over resolved imports only."""

import pytest
from app.analyzers.graph import build_graph
from app.analyzers.graph.rules import check_rules
from app.contracts.architecture_rules import RuleSetV1, RuleV1
from pydantic import ValidationError

from tests.phase3.unit.test_dependency_graph import entries

FILES = {
    "ui/page.py": "from service import logic\n",
    "service/logic.py": "from data import repo\n",
    "data/repo.py": "import json\n",
    "data/bad.py": "from ui import page\n",  # a lower layer importing the top one
    "tools/script.py": "import ghost_module_nobody_has\nfrom . import x\n",
}


def graph(files=FILES):
    return build_graph(entries(files), list(files))


def rule(**kw):
    return RuleSetV1(rules=(RuleV1(name="r", **kw),))


def result(rules, files=FILES):
    return check_rules(graph(files), rules, "id", "d" * 64).results[0]


def test_forbidden_direct_and_indirect_imports_carry_a_chain_and_evidence():
    forbidden = rule(kind="forbidden", groups=(("ui",), ("data",)))
    found = result(forbidden)
    assert found.status == "violated" and found.violations_total == 1
    v = found.violations[0]
    assert v.chain == ("ui/page.py", "service/logic.py", "data/repo.py") and v.evidence is not None
    direct_only = result(rule(kind="forbidden", groups=(("ui",), ("data",)), allow_indirect=True))
    assert direct_only.status == "passed"  # ui reaches data only through service


def test_layers_flag_a_lower_layer_importing_a_higher_one():
    found = result(rule(kind="layers", groups=(("ui",), ("service",), ("data",))))
    assert found.status == "violated" and found.violations_total == 1
    assert (
        found.violations[0].source == "data/bad.py" and found.violations[0].target == "ui/page.py"
    )


def test_independence_checks_every_direction():
    found = result(rule(kind="independence", groups=(("ui",), ("service",))))
    assert found.status == "violated"  # ui imports service
    clean = result(rule(kind="independence", groups=(("tools",), ("service",))))
    assert clean.status in {"passed", "unknown"} and clean.violations_total == 0


def test_unlocated_imports_make_a_clean_result_unknown_not_passed():
    found = result(rule(kind="forbidden", groups=(("tools",), ("data",))))
    assert found.violations_total == 0 and found.unknown_imports >= 1 and found.status == "unknown"


def test_a_passing_rule_with_everything_located_is_passed():
    files = {"a/x.py": "import json\n", "b/y.py": "import os\n"}
    found = result(rule(kind="forbidden", groups=(("a",), ("b",))), files)
    assert found.status == "passed" and found.files_in_scope == 2


def test_rules_are_deterministic_and_bounded():
    forbidden = rule(kind="forbidden", groups=(("ui",), ("data",)))
    g = graph()
    assert check_rules(g, forbidden, "id", "d" * 64) == check_rules(g, forbidden, "id", "d" * 64)
    many = {f"hub/h{i}.py": "from sink import s\n" for i in range(80)} | {"sink/s.py": "x = 1\n"}
    found = result(rule(kind="forbidden", groups=(("hub",), ("sink",))), many)
    assert found.violations_total == 80 and len(found.violations) == 50


@pytest.mark.parametrize(
    "groups,kind",
    [
        ((("a",),), "layers"),  # one group
        ((("a",), ("a",)), "layers"),  # prefix in two groups
        ((("a", "a"), ("b",)), "layers"),  # duplicate prefix
        ((("../x",), ("b",)), "layers"),  # unsafe prefix
        ((("a",), ("b",), ("c",)), "forbidden"),  # forbidden takes exactly two
        (((), ("b",)), "layers"),  # empty group
    ],
)
def test_malformed_rules_are_rejected(groups, kind):
    with pytest.raises(ValidationError):
        RuleV1(name="r", kind=kind, groups=groups)


def test_rule_names_are_unique_and_safe():
    ok = RuleV1(name="a", kind="layers", groups=(("a",), ("b",)))
    with pytest.raises(ValidationError):
        RuleSetV1(rules=(ok, ok))
    with pytest.raises(ValidationError):
        RuleV1(name="bad\nname", kind="layers", groups=(("a",), ("b",)))
