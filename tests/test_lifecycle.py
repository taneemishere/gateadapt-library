"""G3 lifecycle: deferred queue promotion, pairwise interaction probe,
orphan revocation, and drift epochs."""

from gateadapt import Mutation, Verdict
from gateadapt.gate import Gate
from gateadapt.ledger import committed_from_ledger, read_ledger
from conftest import BASE_CFG, make_tasks, scripted_run


def world(seed=1):
    return (
        make_tasks(48, prefix="dev", seed=11),
        make_tasks(48, prefix="probe", fresh=True, seed=23),
    )


def pair_world():
    """needs_pair.a inert alone · needs_pair.b helps alone · both together
    help more, the conditional-interaction fixture."""
    dev, probe = world()
    return Gate(run=scripted_run(), dev=dev, probe=probe, draws=3, seed=1)


IX = lambda: Mutation(id="ix", patch={"needs_pair.a": True}, scope="all")
PB = lambda: Mutation(id="pb", patch={"needs_pair.b": True}, scope="all")


class TestDeferredPromotion:
    def test_inert_abstain_enqueues(self):
        g = pair_world()
        d = g.admit(BASE_CFG, IX())
        assert d.verdict == Verdict.ABSTAIN_WITH_RECORD
        assert len(g.deferred) == 1

    def test_promotes_when_partner_commits(self):
        g = pair_world()
        g.admit(BASE_CFG, IX())
        d = g.admit(BASE_CFG, PB())
        assert d.verdict == Verdict.COMMIT
        # the deferred artifact promoted on the conditional gain,
        # no SAFFRON look spent, the lifecycle instrument found it
        ids = [m.id for m in g.committed]
        assert ids == ["pb", "ix"]
        assert g.promoted_via == {"ix": "pb"}
        assert "ix+pb" in g.interactions
        promotes = [e for e in d.detail["lifecycle"] if e["event"] == "promote"]
        assert promotes and promotes[0]["artifact_id"] == "ix"

    def test_promoted_artifact_records_partner_dependency(self):
        g = pair_world()
        g.admit(BASE_CFG, IX())
        g.admit(BASE_CFG, PB())
        ix = [m for m in g.committed if m.id == "ix"][0]
        assert ix.partner == "pb"

    def test_orphan_revoked_when_partner_revoked(self):
        g = pair_world()
        g.admit(BASE_CFG, IX())
        g.admit(BASE_CFG, PB())
        # drift, where pb's effect inverts on the new probe reserve, and the
        # conditional boost dies with it (it only existed alongside healthy pb)
        g.surface.run = scripted_run(
            pair_b=(-0.40, lambda t: True), pair_ab=(0.0, lambda t: True)
        )
        decisions = g.advance_epoch(
            BASE_CFG,
            probe=make_tasks(48, prefix="probe2", fresh=True, seed=77),
        )
        by_id = {d.artifact_id: d.detail["status"] for d in decisions}
        assert by_id["pb"] == "revoked"
        # ix was conditional on pb, the orphan check removes it too
        assert "ix" not in [m.id for m in g.committed]
        assert "ix" in g.revoked

    def test_no_overlap_region_burns_no_retry(self):
        dev, probe = world()
        g = Gate(
            run=scripted_run(topk_answer=(0.30, lambda t: t.tags.get("type") == "answer")),
            dev=dev, probe=probe, draws=3, seed=1,
        )
        # deferred on refusals, inert solo, waiting on a partner
        g.admit(
            BASE_CFG,
            Mutation(id="ix", patch={"needs_pair.a": True},
                     scope='tags.type == "refusal"'),
        )
        # a refusal-scoped commit overlaps → probed once → one retry burned
        g.admit(
            BASE_CFG,
            Mutation(id="h", patch={"contract": True},
                     scope='tags.type == "refusal"'),
        )
        assert g.deferred[0][1] == 1
        # an answers-scoped commit never overlaps ix's region → no retry
        g.admit(
            BASE_CFG,
            Mutation(id="t", patch={"retrieval.top_k": 8},
                     scope='tags.type == "answer"'),
        )
        assert g.deferred[0][1] == 1


class TestInteractionProbe:
    def test_conflict_revokes_both_sides(self):
        dev, probe = world()
        g = Gate(run=scripted_run(), dev=dev, probe=probe, draws=3, seed=1)
        d1 = g.admit(BASE_CFG, Mutation(id="ca", patch={"conflict.a": True}, scope="all"))
        assert d1.verdict == Verdict.COMMIT
        d2 = g.admit(BASE_CFG, Mutation(id="cb", patch={"conflict.b": True}, scope="all"))
        assert d2.verdict == Verdict.COMMIT
        # joint effect inverts, the pairwise probe revokes both members
        assert g.committed == []
        assert set(g.revoked) == {"ca", "cb"}
        revokes = [e for e in d2.detail["lifecycle"] if e["event"] == "revoke"]
        assert {e["artifact_id"] for e in revokes} == {"ca", "cb"}

    def test_positive_interaction_detected_not_duplicated(self):
        g = pair_world()
        g.admit(BASE_CFG, IX())
        g.admit(BASE_CFG, PB())
        # ix promoted, the pair is recorded once under the canonical order,
        # not once per direction
        assert g.interactions == ["ix+pb"]


class TestDriftEpoch:
    def test_shift_namespaces_cache(self):
        dev, probe = world()
        g = Gate(run=scripted_run(), dev=dev, probe=probe, draws=3, seed=1)
        name0 = g.surface.name
        g.shift(probe=make_tasks(48, prefix="p2", fresh=True, seed=5))
        assert g.surface.name != name0

    def test_advance_epoch_audits_on_new_probe(self):
        dev, probe = world()
        g = Gate(run=scripted_run(), dev=dev, probe=probe, draws=3, seed=1)
        g.admit(BASE_CFG, Mutation(id="c", patch={"contract": True},
                                   scope='tags.type == "refusal"'))
        decisions = g.advance_epoch(BASE_CFG, probe=probe[:])
        assert decisions[0].detail["status"] in ("kept", "revoked", "kept_scope_starved")

    def test_drifted_commit_revoked_at_epoch(self):
        dev, probe = world()
        g = Gate(run=scripted_run(), dev=dev, probe=probe, draws=3, seed=1)
        g.admit(BASE_CFG, Mutation(id="c", patch={"contract": True},
                                   scope='tags.type == "refusal"'))
        # the world drifts, contract now hurts refusals
        g.surface.run = scripted_run(contract=(-0.50, lambda t: t.tags.get("type") == "refusal"))
        decisions = g.advance_epoch(
            BASE_CFG,
            probe=make_tasks(48, prefix="probe3", fresh=True, seed=91),
        )
        assert decisions[0].detail["status"] == "revoked"
        assert g.committed == []

    def test_scope_starved_region_kept(self):
        dev, probe = world()
        g = Gate(run=scripted_run(), dev=dev, probe=probe, draws=3, seed=1)
        g.admit(BASE_CFG, Mutation(id="c", patch={"contract": True},
                                   scope='tags.type == "refusal"'))
        # new probe has no refusals, starved is recorded, not revoked
        answers_only = [t for t in probe if t.tags.get("type") == "answer"]
        decisions = g.advance_epoch(BASE_CFG, probe=answers_only)
        assert decisions[0].detail["status"] == "kept_scope_starved"
        assert len(g.committed) == 1


class TestLedgerLifecycle:
    def test_promote_and_revoke_rebuild_committed(self, tmp_path):
        dev, probe = world()
        lp = str(tmp_path / "ledger.jsonl")
        g = Gate(run=scripted_run(), dev=dev, probe=probe, draws=3, seed=1,
                 ledger_path=lp)
        g.admit(BASE_CFG, IX())
        g.admit(BASE_CFG, PB())
        committed = committed_from_ledger(lp)
        assert {m.id for m in committed} == {"pb", "ix"}
        # the promoted artifact's partner dependency survives the rebuild
        ix = [m for m in committed if m.id == "ix"][0]
        assert ix.partner == "pb"
        kinds = [e["event"] for e in read_ledger(lp)]
        assert kinds.count("verdict") == 3  # abstain + commit + promoted commit


class TestDeferredBounded:
    def test_retries_capped(self):
        dev, probe = world()
        g = Gate(
            run=scripted_run(topk_answer=(0.30, lambda t: t.tags.get("type") == "answer")),
            dev=dev, probe=probe, draws=3, seed=1,
        )
        g.admit(BASE_CFG, IX())
        # overlapping-but-non-unlocking commits burn one probed retry each;
        # needs_pair.b is deliberately absent or ix would promote
        for mut in [
            Mutation(id="c", patch={"contract": True}, scope='tags.type == "refusal"'),
            Mutation(id="p", patch={"prune": True}, scope="all"),
            Mutation(id="ca", patch={"conflict.a": True}, scope="all"),
            Mutation(id="b1", patch={"boost": True}, scope="all"),
            Mutation(id="b2", patch={"boost": True}, scope='tags.type == "refusal"'),
        ]:
            g.admit(BASE_CFG, mut)
        # probed on every commit, capped at MAX_DEFERRED_RETRIES, never promoted
        assert g.deferred[0][1] == 4
        assert "ix" not in [m.id for m in g.committed]
