from gateadapt.replay import replay


def test_replay_9b_verdicts():
    res = replay("9b")
    verdicts = {r.artifact: r.verdict for r in res["rows"]}
    # +0.30 on 20 items is 7 wins 1 loss, exact McNemar p 0.070. An
    # unchanged rerun already flips 6 of 20 tasks, so the edit AutoHarness
    # kept can't be certified at this size; the paired z-test overclaimed it
    assert verdicts["9b-r1"] == "abstain_with_record"
    assert verdicts["9b-r2"] == "abstain_with_record"
    assert verdicts["9b-r3"] == "abstain_with_record"
    assert res["noise"]["flips"] > 0  # the noise floor is real


def test_replay_27b_verdicts():
    res = replay("27b")
    verdicts = {r.artifact: r.verdict for r in res["rows"]}
    assert verdicts["27b-r1"] == "commit"
    # r2/r3, AutoHarness kept edits their own noise check can't certify,
    # the shared-pool dev look honestly abstains (matches the research readout)
    assert verdicts["27b-r2"] == "abstain_with_record"
    assert verdicts["27b-r3"] == "abstain_with_record"
