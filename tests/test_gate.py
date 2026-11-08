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

    def test_scope_correct_preserves_partner(self, world, tmp_path):
        """A declared partner dependency survives scope correction, the
        committed artifact's to_dict carries it into the ledger."""
        dev, probe, _ = world
        lp = str(tmp_path / "ledger.jsonl")
        g = gate(dev, probe, ledger=lp)
        d = g.admit(BASE_CFG, Mutation(id="pr", patch={"prune": True}, scope="all",
                                       partner="pb"))
        assert d.verdict == Verdict.SCOPE_CORRECT
        art = [e["artifact"] for e in read_ledger(lp)
               if e["event"] == "verdict" and e["artifact"]["id"] == "pr"][0]
        assert art["partner"] == "pb"

    def test_same_id_second_commit_replaces(self, world):
        """Re-admitting a committed id replaces it, matching the ledger
        rebuild's dedup-by-id instead of stacking two same-id artifacts."""
        dev, probe, _ = world
        g = gate(dev, probe)
        d1 = g.admit(BASE_CFG, Mutation(id="x", patch={"contract": True},
                                        scope='tags.type == "refusal"'))
        assert d1.verdict == Verdict.COMMIT
        d2 = g.admit(BASE_CFG, Mutation(id="x", patch={"boost": True}, scope="all"))
        assert d2.verdict == Verdict.COMMIT
        same_id = [m for m in g.committed if m.id == "x"]
        assert len(same_id) == 1
        assert same_id[0].scope_ast == ("all",)

    def test_harm_sign_branch_needs_magnitude(self):
        """Consistent direction without size is not harm: a near-inert
        effect can't convict a mutation."""
        from gateadapt.gate import _harm
        from gateadapt.stats import PairedResult

        assert not _harm(PairedResult(4, 0, 4, -0.005, 0.001, []))
        assert _harm(PairedResult(4, 0, 4, -0.02, 0.005, []))

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

    def test_callable_scope_inconclusive_skips_scan(self, world):
        """A callable scope has no AST to refine. The scan's inside arm would
        measure tasks the callable never fires on, so it must be a no-op."""
        dev, probe, _ = world
        g_call = gate(dev, probe)
        d = g_call.admit(BASE_CFG, Mutation(id="w", patch={"lift": True}, scope=lambda t: True))
        assert d.verdict == Verdict.ABSTAIN_WITH_RECORD
        assert "narrowed_scope" not in d.detail
        # the same mutation on the AST 'all' scope takes the inconclusive
        # path into the refinement scan and spends episodes on it; the
        # callable must not
        g_ast = gate(dev, probe)
        g_ast.admit(BASE_CFG, Mutation(id="w", patch={"lift": True}, scope="all"))
        assert g_call.surface.episodes < g_ast.surface.episodes


class TestAudit:
    def test_revoke_on_drifted_effect(self, world):
        dev, probe, _ = world
        run = scripted_run()
        g = Gate(run=run, dev=dev, probe=probe, draws=3, seed=1)
        g.admit(BASE_CFG, Mutation(id="c", patch={"contract": True}, scope='tags.type == "refusal"'))
        assert len(g.committed) == 1
        # the world shifts, contract now hurts
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

    def test_gate_loads_committed_from_ledger(self, world, tmp_path):
        """A ledger path is load-bearing: the gate rebuilds the committed
        set itself, stacked admission does not depend on the caller."""
        dev, probe, _ = world
        lp = str(tmp_path / "ledger.jsonl")
        g = gate(dev, probe, ledger=lp)
        g.admit(BASE_CFG, Mutation(id="c", patch={"contract": True},
                                   scope='tags.type == "refusal"'))
        g2 = gate(dev, probe, ledger=lp)
        assert [m.id for m in g2.committed] == ["c"]

    def test_same_id_reject_keeps_committed_rebuild(self, world, tmp_path):
        """A later reject of a re-submitted id judges the candidate, not the
        committed set. Rebuild must match what the live gate kept."""
        dev, probe, _ = world
        lp = str(tmp_path / "ledger.jsonl")
        g = gate(dev, probe, ledger=lp)
        g.admit(BASE_CFG, Mutation(id="x", patch={"contract": True},
                                   scope='tags.type == "refusal"'))
        d = g.admit(BASE_CFG, Mutation(id="x", patch={"always_answer": True},
                                      scope='tags.type == "refusal"'))
        assert d.verdict == Verdict.REJECT
        assert [m.id for m in g.committed] == ["x"]
        assert [m.id for m in committed_from_ledger(lp)] == ["x"]
