from gateadapt import FeatureSpec, Mutation, Task
from gateadapt.gate import g0_check
from conftest import make_tasks

TASKS = make_tasks(24)
SPEC = FeatureSpec(fields={"tags.type": str, "tags.domain": str})
DEV_IDS = {t.id for t in TASKS}


def check(m):
    return g0_check(m, SPEC, DEV_IDS)


def test_clean_mutation_passes():
    m = Mutation(id="m", patch={"a": 1}, scope='tags.type == "refusal"')
    assert check(m) == []


def test_unknown_scope_field_rejected():
    m = Mutation(id="m", patch={"a": 1}, scope='tags.bogus == "x"')
    assert any("unknown feature" in v for v in check(m))


def test_stateful_kind_needs_global_scope():
    m = Mutation(id="m", kind="memory_rule", patch={"a": 1}, scope='tags.type == "refusal"')
    assert any("must declare scope 'all'" in v for v in check(m))
    m2 = Mutation(id="m", kind="memory_rule", patch={"a": 1}, scope="all")
    assert check(m2) == []


def test_stateful_callable_scope_rejected():
    m = Mutation(id="m", kind="budget_rule", patch={"a": 1}, scope=lambda t: True)
    assert any("stateful" in v for v in check(m))


def test_leaked_ids_rejected():
    m = Mutation(id="m", patch={"a": 1}, scope="all",
                 payload={"hint": "memorize d-003 and d-007"})
    assert any("leaks" in v for v in check(m))
    m2 = Mutation(id="m", patch={"key": "answer is d-004"}, scope="all")
    assert any("leaks" in v for v in check(m2))


def test_leak_check_ignores_substrings():
    # an id buried inside other words or suffixed is a different token, not
    # a leak
    m = Mutation(id="m", patch={"a": 1}, scope="all",
                 payload={"hint": "ad-003 is not the task id"})
    assert not any("leaks" in v for v in check(m))
    m2 = Mutation(id="m", patch={"a": 1}, scope="all",
                  payload={"hint": "d-003x is a different id"})
    assert not any("leaks" in v for v in check(m2))


def test_empty_mutation_rejected():
    m = Mutation(id="m", scope="all")
    assert any("nothing to admit" in v for v in check(m))


def test_callable_scope_stateless_ok():
    m = Mutation(id="m", patch={"a": 1}, scope=lambda t: True)
    assert check(m) == []
