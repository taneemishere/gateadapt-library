import math

from gateadapt import Task, eval_pair, sign_test_p, tasks_needed
from gateadapt.stats import fisher_p


def test_eval_pair_pairs_items():
    items = [Task(id=str(i)) for i in range(6)]
    effects = {str(i): (0.0, 1.0) for i in range(4)}  # wins
    effects.update({str(i): (1.0, 0.0) for i in (4, 5)})  # losses
    res = eval_pair(items, lambda it: effects[it.id])
    assert res.n == 6 and res.wins == 4 and res.losses == 2
    assert abs(res.diff - (4 - 2) / 6) < 1e-9
    assert res.diff_se > 0


def test_eval_pair_empty():
    assert eval_pair([], lambda it: (0, 0)).n == 0


def test_degenerate_all_wins_is_significant():
    """se=0 must not read p=1: an all-wins run is the most significant
    outcome, resolved by the exact sign test (caught by the live substrate,
    a +1.0 unanimous effect abstained under the old rule)."""
    items = [Task(id=str(i)) for i in range(7)]
    m = eval_pair(items, lambda it: (0.0, 1.0))
    assert m.diff_se == 0.0 and m.p < 0.025
    items_tie = items[:5]
    assert eval_pair(items_tie, lambda it: (0.5, 0.5)).p == 1.0


def test_sign_test_p():
    assert sign_test_p(0, 0) == 1.0
    assert sign_test_p(9, 1) < 0.05
    assert sign_test_p(5, 5) == 1.0


def test_ztest_p_significant():
    items = [Task(id=str(i)) for i in range(20)]
    res = eval_pair(items, lambda it: (0.0, 1.0) if int(it.id) < 15 else (1.0, 1.0))
    assert res.p < 0.001


def test_tasks_needed_monotone():
    assert tasks_needed(0.10) > tasks_needed(0.20) > tasks_needed(0.40)
    assert tasks_needed(0.0) == 0
    assert tasks_needed(0.10, power=0.9) > tasks_needed(0.10, power=0.5)


def test_fisher_p_combines():
    assert fisher_p(0.05, 0.05) < 0.05
    assert fisher_p(0.5, 0.5) > 0.3
    assert 0 < fisher_p(0.0, 1.0) < 1
