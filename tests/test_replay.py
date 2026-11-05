from gateadapt.replay import replay


def test_replay_9b_verdicts():
    res = replay("9b")
    verdicts = {r.artifact: r.verdict for r in res["rows"]}
    assert verdicts["9b-r1"] == "commit"
    assert verdicts["9b-r2"] == "abstain_with_record"
    assert verdicts["9b-r3"] == "reject"
    assert res["noise"]["flips"] > 0  # the noise floor is real


def test_replay_27b_verdicts():
    res = replay("27b")
    verdicts = {r.artifact: r.verdict for r in res["rows"]}
    assert verdicts["27b-r1"] == "commit"
    # r2/r3: AutoHarness kept edits their own noise check can't certify —
    # the shared-pool dev look honestly abstains (matches the research readout)
    assert verdicts["27b-r2"] == "abstain_with_record"
    assert verdicts["27b-r3"] == "abstain_with_record"
