"""G2 direction-blind synthesis: shared SAFFRON budget, cumulative test pool,
reserve deck / generator item sources, probe still gates the final commit."""

import random

from gateadapt import Mutation, Task, Verdict
from gateadapt.gate import Gate
from conftest import BASE_CFG, make_tasks, scripted_run


def answers(n, prefix, seed, fresh=False):
    """Tag-poor world: every task is an answer with no split-worthy fields,
    nothing for the heterogeneity scan to refine, so synthesis runs on the
    declared scope itself."""
    rng = random.Random(seed)
    tags = {"type": "answer"}
    if fresh:
        tags = dict(tags, fresh=True)
    return [
        Task(id=f"{prefix}-{i:03d}", tags=dict(tags), data={"p": round(rng.uniform(0.25, 0.75), 3)})
        for i in range(n)
    ]


def lift(eff):
    return scripted_run(topk_answer=(eff, lambda t: True))


def gate(dev, probe=None, reserve=None, generate=None, effect=0.20, **kw):
    return Gate(
        run=lift(effect),
        dev=dev,
        probe=probe,
        reserve=reserve,
        generate=generate,
        seed=1,
        items_per_round=24,
        **kw,
    )


LIFT_MUT = lambda: Mutation(id="tk", patch={"retrieval.top_k": 8}, scope="all")


class TestSynthesis:
    def test_commit_via_cumulative_test_pool(self):
        dev, probe = answers(24, "dev", 11), answers(24, "probe", 23)
        g = gate(list(dev), list(probe), answers(320, "res", 53), effect=0.20)
        d = g.admit(BASE_CFG, LIFT_MUT())
        assert d.verdict == Verdict.COMMIT
        rounds = d.detail["synthesis_rounds"]
        assert [r["k"] for r in rounds] == [1, 2]
        assert rounds[0]["verdict"] == "inconclusive"
        assert rounds[1]["verdict"] == "commit"
        # the test pool is cumulative across rounds
        assert rounds[0]["test_n"] < rounds[1]["test_n"]
        assert len(g.committed) == 1

    def test_abstains_when_synthesis_cannot_resolve(self):
        dev = answers(24, "dev", 11)
        g = gate(list(dev), reserve=answers(320, "res", 53), effect=0.15)
        d = g.admit(BASE_CFG, LIFT_MUT())
        assert d.verdict == Verdict.ABSTAIN_WITH_RECORD
        rounds = d.detail["synthesis_rounds"]
        assert len(rounds) == 3  # looks-1 synthesis rounds after the dev look
        assert all(r["verdict"] == "inconclusive" for r in rounds)
        assert not g.committed

    def test_dev_deck_grows_with_synthesized_items(self):
        dev = answers(24, "dev", 11)
        g = gate(list(dev), reserve=answers(320, "res", 53), effect=0.20)
        g.admit(BASE_CFG, LIFT_MUT())
        # commit at round 2, two rounds of 24 fresh items joined dev
        assert len(g.dev.items) == 24 + 48

    def test_reserve_drawn_without_replacement(self):
        dev = answers(24, "dev", 11)
        reserve = answers(320, "res", 53)
        g = gate(list(dev), reserve=reserve, effect=0.20)
        g.admit(BASE_CFG, LIFT_MUT())
        drawn_ids = {t.id for t in g.dev.items if t.id.startswith("res-")}
        remaining_ids = {t.id for t in g._reserve}
        assert len(drawn_ids) == 48
        assert not (drawn_ids & remaining_ids)

    def test_generator_callable_as_item_source(self):
        calls = []

        def generate(region_fn, n, rng):
            calls.append(n)
            out = []
            for i in range(n):
                t = Task(
                    id=f"syn-{rng.randrange(10**6)}",
                    tags={"type": "answer"},
                    data={"p": round(0.3 + rng.random() * 0.4, 3)},
                )
                if region_fn(t):
                    out.append(t)
            return out

        dev = answers(24, "dev", 11)
        g = gate(list(dev), generate=generate, effect=0.20)
        d = g.admit(BASE_CFG, LIFT_MUT())
        assert d.verdict == Verdict.COMMIT
        assert calls  # the synthesizer was asked for fresh in-region items

    def test_no_item_source_still_abstains(self):
        dev = answers(24, "dev", 11)
        g = gate(list(dev), reserve=None, generate=None, effect=0.20)
        d = g.admit(BASE_CFG, LIFT_MUT())
        assert d.verdict == Verdict.ABSTAIN_WITH_RECORD
        assert "synthesis_rounds" not in d.detail

    def test_reserve_exhaustion_stops_drawing(self):
        dev = answers(24, "dev", 11)
        g = gate(list(dev), reserve=answers(30, "res", 53), effect=0.15)
        d = g.admit(BASE_CFG, LIFT_MUT())
        assert d.verdict == Verdict.ABSTAIN_WITH_RECORD
        total_drawn = sum(r["items_drawn"] for r in d.detail["synthesis_rounds"])
        assert total_drawn <= 30

    def test_transfer_probe_still_gates_synthesis_commit(self):
        dev, probe = answers(24, "dev", 11), answers(24, "probe", 23, fresh=True)
        g = Gate(
            run=scripted_run(memoize_seen=(0.20, lambda t: not t.tags.get("fresh"))),
            dev=list(dev),
            probe=list(probe),
            reserve=answers(320, "res", 53),
            seed=1,
            items_per_round=24,
        )
        d = g.admit(BASE_CFG, Mutation(id="m", patch={"memoize_dev": True}, scope="all"))
        assert d.verdict == Verdict.REJECT
        assert d.detail["caught_by"] == "transfer_probe"
        # synthesis had resolved commit before the probe fired
        assert d.detail["synthesis_rounds"][-1]["verdict"] == "commit"

    def test_family_budget_exhaustion_blocks_next_member(self):
        dev = answers(24, "dev", 11)
        g = Gate(
            run=scripted_run(),
            dev=list(dev),
            reserve=answers(320, "res", 77),
            seed=1,
            items_per_round=24,
        )
        # an inert family member burns all four looks inconclusively
        d1 = g.admit(BASE_CFG, Mutation(id="i1", patch={"noop": 1}, scope="all", family="f"))
        assert d1.verdict == Verdict.ABSTAIN_WITH_RECORD
        # the family's pool is drained, the next member cannot afford look 1
        d2 = g.admit(BASE_CFG, Mutation(id="i2", patch={"noop": 2}, scope="all", family="f"))
        assert d2.verdict == Verdict.ABSTAIN_WITH_RECORD
        assert d2.detail["reason"] == "family_budget_exhausted"
        # a different family gets its own pool
        d3 = g.admit(BASE_CFG, Mutation(id="i3", patch={"noop": 3}, scope="all"))
        assert d3.detail.get("reason") != "family_budget_exhausted"

    def test_decided_member_refunds_look(self):
        dev, probe = answers(24, "dev", 11), answers(24, "probe", 23)
        g = gate(list(dev), list(probe), answers(320, "res", 53), effect=0.30)
        g.admit(
            BASE_CFG,
            Mutation(id="tk1", patch={"retrieval.top_k": 8}, scope="all", family="liftfam"),
        )
        pool = g._pools["liftfam"]
        # the decided dev look refunded its quarter, full wealth remains
        assert pool.remaining_wealth > 0.09

    def test_deterministic_across_runs(self):
        verdicts = []
        for _ in range(2):
            g = gate(answers(24, "dev", 11), reserve=answers(320, "res", 53), effect=0.20)
            d = g.admit(BASE_CFG, LIFT_MUT())
            verdicts.append((d.verdict, len(d.detail["synthesis_rounds"])))
        assert verdicts[0] == verdicts[1]
