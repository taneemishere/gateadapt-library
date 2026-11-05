import json

import pytest

from gateadapt import (
    Deck,
    DeckRole,
    HonestyViolation,
    Mutation,
    Task,
    Verdict,
    load_tasks,
)
from gateadapt.gate import Gate
from gateadapt.ledger import committed_from_ledger, read_ledger
from conftest import BASE_CFG, make_tasks, scripted_run


@pytest.fixture
def world(tmp_path):
    dev = make_tasks(48, prefix="dev", seed=11)
    probe = make_tasks(48, prefix="probe", fresh=True, seed=23)
    audit = make_tasks(60, prefix="aud", seed=37)
    return dev, probe, audit


def gate(dev, probe=None, audit=None, ledger=None, draws=3, seed=1, **fx):
    return Gate(
        run=scripted_run(**fx),
        dev=dev,
        probe=probe,
        audit=audit,
        draws=draws,
        seed=seed,
        ledger_path=ledger,
    )


class TestAdmit:
    def test_commit_clear_help(self, world):
        dev, probe, audit = world
        g = gate(dev, probe)
        d = g.admit(BASE_CFG, Mutation(id="c", patch={"contract": True}, scope='tags.type == "refusal"'))
        assert d.verdict == Verdict.COMMIT
        assert d.detail["diff"] > 0 and d.detail["p"] < 0.05
        assert len(g.committed) == 1

    def test_reject_clear_harm(self, world):
        dev, probe, _ = world
        g = gate(dev, probe)
        d = g.admit(BASE_CFG, Mutation(id="h", patch={"always_answer": True}, scope="all"))
        assert d.verdict == Verdict.REJECT
        assert d.detail["diff"] < 0
        assert not g.committed

    def test_abstain_on_underpowered(self, world):
        dev, probe, _ = world
        g = gate(dev, probe)
        d = g.admit(
            BASE_CFG,
            Mutation(id="tk", patch={"retrieval.top_k": 8}, scope='tags.type == "answer"'),
        )
        assert d.verdict == Verdict.ABSTAIN_WITH_RECORD
        assert "needed" in d.detail and d.detail["needed"]["need"] > 0
        assert not g.committed

    def test_abstain_on_empty_region(self, world):
        dev, probe, _ = world
        g = gate(dev, probe)
        d = g.admit(BASE_CFG, Mutation(id="x", patch={"a": 1}, scope='tags.type == "ghost"'))
        assert d.verdict == Verdict.ABSTAIN_WITH_RECORD
        assert d.detail["region_n"] == 0

    def test_scope_correct_on_misdeclared_scope(self, world):
        dev, probe, _ = world
        g = gate(dev, probe)
        d = g.admit(BASE_CFG, Mutation(id="pr", patch={"prune": True}, scope="all"))
        assert d.verdict == Verdict.SCOPE_CORRECT
        narrowed = g.committed[0].scope_ast
        # the committed scope must mention the acme domain, not the world
        assert ("eq", "tags.domain", "acme") in narrowed
        assert d.detail["heterogeneity"]["diff_in"] > d.detail["heterogeneity"]["diff_out"]

    def test_transfer_probe_catches_shortcut(self, world):
        dev, probe, _ = world
        g = gate(dev, probe)
        d = g.admit(BASE_CFG, Mutation(id="sc", patch={"memoize_dev": True}, scope="all"))
        assert d.verdict == Verdict.REJECT
        assert d.detail["caught_by"] == "transfer_probe"
        assert not g.committed

    def test_shortcut_commits_without_probe(self, world):
        # the probe deck is what stands between "helps dev" and "harms fresh"
        dev, _, _ = world
        g = gate(dev, probe=None)
        d = g.admit(BASE_CFG, Mutation(id="sc", patch={"memoize_dev": True}, scope="all"))
        assert d.verdict == Verdict.COMMIT
        assert "skipped" in d.detail["transfer_probe"]

    def test_g0_reject_does_not_measure(self, world):
        dev, _, _ = world
        g = gate(dev)
        run_calls = g.surface.episodes
        d = g.admit(BASE_CFG, Mutation(id="leak", patch={"k": "see dev-000"}, scope="all"))
        assert d.verdict == Verdict.REJECT
        assert "g0_violations" in d.detail
        assert g.surface.episodes == run_calls

    def test_stacked_admission(self, world):
        dev, probe, _ = world
        g = gate(dev, probe)
        g.admit(BASE_CFG, Mutation(id="c", patch={"contract": True}, scope='tags.type == "refusal"'))
        # second mutation measured on top of the committed harness
        m2 = Mutation(id="tk", patch={"retrieval.top_k": 8}, scope='tags.type == "answer"')
        g.admit(BASE_CFG, m2)
        # a mutation whose effect conflicts with the committed stack
        m3 = Mutation(id="h", patch={"always_answer": True}, scope="all")
        d3 = g.admit(BASE_CFG, m3)
        assert d3.verdict == Verdict.REJECT

    def test_callable_scope_admits(self, world):
        dev, probe, _ = world
        g = gate(dev, probe)
        d = g.admit(
            BASE_CFG,
            Mutation(id="cc", patch={"contract": True}, scope=lambda t: t.tags.get("type") == "refusal"),
        )
        assert d.verdict == Verdict.COMMIT


class TestAudit:
    def test_revoke_on_drifted_effect(self, world):
        dev, probe, _ = world
        run = scripted_run()
        g = Gate(run=run, dev=dev, probe=probe, draws=3, seed=1)
        g.admit(BASE_CFG, Mutation(id="c", patch={"contract": True}, scope='tags.type == "refusal"'))
        assert len(g.committed) == 1
        # the world shifts: contract now hurts
        g2 = Gate(
            run=scripted_run(contract=(-0.40, lambda t: t.tags.get("type") == "refusal")),
            dev=dev, probe=probe, draws=3, seed=1,
        )
        g2.committed = list(g.committed)
        decisions = g2.audit(BASE_CFG)
        assert decisions[0].detail["status"] == "revoked"
        assert not g2.committed

    def test_keeps_healthy_commits(self, world):
        dev, probe, audit = world
        g = gate(dev, probe, audit)
        g.admit(BASE_CFG, Mutation(id="c", patch={"contract": True}, scope='tags.type == "refusal"'))
        decisions = g.audit(BASE_CFG)
        assert decisions[0].detail["status"] == "kept"

    def test_audit_report_on_frozen_deck(self, world):
        dev, probe, audit = world
        g = gate(dev, probe, audit)
        g.admit(BASE_CFG, Mutation(id="c", patch={"contract": True}, scope='tags.type == "refusal"'))
        rep = g.audit_report(BASE_CFG)
        assert rep["n"] > 0 and rep["diff"] > 0


class TestLedger:
    def test_events_written(self, world, tmp_path):
        dev, probe, _ = world
        lp = str(tmp_path / "ledger.jsonl")
        g = gate(dev, probe, ledger=lp)
        g.admit(BASE_CFG, Mutation(id="c", patch={"contract": True}, scope='tags.type == "refusal"'))
        g.admit(BASE_CFG, Mutation(id="h", patch={"always_answer": True}, scope="all"))
        events = read_ledger(lp)
        assert [e["event"] for e in events] == ["verdict", "verdict"]
        assert events[0]["verdict"] == "commit" and events[1]["verdict"] == "reject"

    def test_committed_set_rebuilds(self, world, tmp_path):
        dev, probe, _ = world
        lp = str(tmp_path / "ledger.jsonl")
        g = gate(dev, probe, ledger=lp)
        g.admit(BASE_CFG, Mutation(id="c", patch={"contract": True}, scope='tags.type == "refusal"'))
        g.admit(BASE_CFG, Mutation(id="h", patch={"always_answer": True}, scope="all"))
        committed = committed_from_ledger(lp)
        assert [m.id for m in committed] == ["c"]
        # commit then revoke -> empty
        g.committed = []
        g2 = Gate(run=scripted_run(contract=(-0.4, lambda t: True)), dev=dev, draws=3, ledger_path=lp)
        g2.committed = committed
        g2.audit(BASE_CFG)
        assert committed_from_ledger(lp) == []
