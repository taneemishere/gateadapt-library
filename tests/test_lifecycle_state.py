"""Explicit lifecycle state: the artifact_state read model, state_after in
decisions and ledger events, rebuild parity across processes, status CLI."""

import json

from gateadapt import Gate, LifecycleState, Mutation, Verdict
from gateadapt.cli import main as cli_main
from gateadapt.gate import _MAX_DEFERRED_RETRIES
from conftest import BASE_CFG, make_tasks, scripted_run
from test_lifecycle import IX, PB, pair_world, world


def _ledger_gate(tmp_path, **kw):
    dev, probe = world()
    return Gate(
        run=scripted_run(),
        dev=dev,
        probe=probe,
        seed=1,
        ledger_path=str(tmp_path / "ledger.jsonl"),
        **kw,
    )


class TestArtifactState:
    def test_committed(self):
        g = pair_world()
        g.admit(BASE_CFG, PB())
        assert g.artifact_state("pb")["state"] == LifecycleState.COMMITTED

    def test_conditional_after_promotion(self):
        g = pair_world()
        g.admit(BASE_CFG, IX())
        g.admit(BASE_CFG, PB())
        st = g.artifact_state("ix")
        assert st["state"] == LifecycleState.CONDITIONAL
        assert st["partner"] == "pb"

    def test_deferred_with_retry_count(self):
        g = pair_world()
        g.admit(BASE_CFG, IX())
        st = g.artifact_state("ix")
        assert st["state"] == LifecycleState.DEFERRED
        assert st["retries"] == 0
        assert st["max_retries"] == _MAX_DEFERRED_RETRIES

    def test_expired_at_retry_cap(self):
        g = pair_world()
        g.admit(BASE_CFG, IX())
        g.deferred[0][1] = _MAX_DEFERRED_RETRIES
        assert g.artifact_state("ix")["state"] == LifecycleState.EXPIRED

    def test_revoked(self):
        g = pair_world()
        g.admit(BASE_CFG, PB())
        g._revoke(g.committed[0], "drift_harm", {})
        assert g.artifact_state("pb")["state"] == LifecycleState.REVOKED

    def test_rejected_distinguishable_from_unknown(self):
        g = pair_world()
        g.admit(BASE_CFG, Mutation(id="aa", patch={"always_answer": True}, scope="all"))
        assert g.artifact_state("aa")["state"] == LifecycleState.REJECTED
        assert g.artifact_state("never-seen")["state"] == LifecycleState.UNKNOWN

    def test_live_state_wins_over_history(self):
        # a revoked id re-committed reads committed, not revoked
        g = pair_world()
        g.admit(BASE_CFG, PB())
        g._revoke(g.committed[0], "drift_harm", {})
        g._admit_committed(PB())
        assert g.artifact_state("pb")["state"] == LifecycleState.COMMITTED


class TestStateAfter:
    def test_commit_decision(self):
        g = pair_world()
        d = g.admit(BASE_CFG, PB())
        assert d.detail["state_after"] == "committed"

    def test_abstain_decision(self):
        g = pair_world()
        d = g.admit(BASE_CFG, IX())
        assert d.detail["state_after"] == "deferred"

    def test_reject_decision(self):
        g = pair_world()
        d = g.admit(BASE_CFG, Mutation(id="aa", patch={"always_answer": True}, scope="all"))
        assert d.detail["state_after"] == "rejected"

    def test_promotion_ledger_event(self):
        g = pair_world()
        g.admit(BASE_CFG, IX())
        d = g.admit(BASE_CFG, PB())
        promotes = [e for e in d.detail["lifecycle"] if e["event"] == "promote"]
        assert promotes


class TestRebuildParity:
    def test_states_survive_restart(self, tmp_path):
        g1 = _ledger_gate(tmp_path)
        g1.admit(BASE_CFG, IX())
        g1.admit(BASE_CFG, Mutation(id="aa", patch={"always_answer": True}, scope="all"))
        g1.admit(BASE_CFG, PB())
        g2 = Gate(
            run=scripted_run(),
            dev=make_tasks(48, prefix="dev", seed=11),
            probe=make_tasks(48, prefix="probe", fresh=True, seed=23),
            seed=1,
            ledger_path=str(tmp_path / "ledger.jsonl"),
        )
        assert g2.artifact_state("pb")["state"] == LifecycleState.COMMITTED
        assert g2.artifact_state("ix")["state"] == LifecycleState.CONDITIONAL
        assert g2.artifact_state("aa")["state"] == LifecycleState.REJECTED

    def test_deferred_survives_restart(self, tmp_path):
        g1 = _ledger_gate(tmp_path)
        g1.admit(BASE_CFG, IX())
        g2 = Gate(
            run=scripted_run(),
            dev=make_tasks(48, prefix="dev", seed=11),
            probe=make_tasks(48, prefix="probe", fresh=True, seed=23),
            seed=1,
            ledger_path=str(tmp_path / "ledger.jsonl"),
        )
        assert g2.artifact_state("ix")["state"] == LifecycleState.DEFERRED
        # and the deferred artifact can still promote in the new process
        d = g2.admit(BASE_CFG, PB())
        promotes = [e for e in d.detail["lifecycle"] if e["event"] == "promote"]
        assert promotes and promotes[0]["artifact_id"] == "ix"

    def test_revoked_survives_restart(self, tmp_path):
        g1 = _ledger_gate(tmp_path)
        g1.admit(BASE_CFG, PB())
        g1._revoke(g1.committed[0], "drift_harm", {})
        g2 = Gate(
            run=scripted_run(),
            dev=make_tasks(48, prefix="dev", seed=11),
            probe=make_tasks(48, prefix="probe", fresh=True, seed=23),
            seed=1,
            ledger_path=str(tmp_path / "ledger.jsonl"),
        )
        assert g2.artifact_state("pb")["state"] == LifecycleState.REVOKED

    def test_ledger_verdict_carries_state_after(self, tmp_path):
        g1 = _ledger_gate(tmp_path)
        g1.admit(BASE_CFG, PB())
        events = [json.loads(l) for l in (tmp_path / "ledger.jsonl").read_text().splitlines()]
        verdict = next(e for e in events if e.get("event") == "verdict")
        assert verdict["detail"]["state_after"] == "committed"


class TestStatusCli:
    def test_status_table(self, tmp_path, capsys):
        g1 = _ledger_gate(tmp_path)
        g1.admit(BASE_CFG, IX())
        g1.admit(BASE_CFG, PB())
        g1.admit(BASE_CFG, Mutation(id="aa", patch={"always_answer": True}, scope="all"))
        capsys.readouterr()
        cli_main(["status", "--ledger", str(tmp_path / "ledger.jsonl")])
        out = capsys.readouterr().out
        assert "committed" in out and "pb" in out
        assert "conditional" in out and "ix" in out
        assert "rejected" in out and "aa" in out

    def test_status_json(self, tmp_path, capsys):
        g1 = _ledger_gate(tmp_path)
        g1.admit(BASE_CFG, PB())
        capsys.readouterr()
        cli_main(["status", "--ledger", str(tmp_path / "ledger.jsonl"), "--json"])
        states = json.loads(capsys.readouterr().out)
        assert states["pb"]["state"] == "committed"
