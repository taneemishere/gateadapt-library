"""Statistical behaviour pinned as regression tests.

Seeded simulations on a synthetic binary agent where the true effect is
known: no-effect admits must decide at or under nominal alpha, a real
uniform gain must not be narrowed by noise, a healthy change must survive
re-audit, and the input holes (duplicate ids, shared deck ids, dev-only
gains) must not produce commits. Every run is deterministic.
"""

import hashlib
from collections import Counter

import pytest

from gateadapt import Gate, Mutation, Task, Verdict

M = Mutation(id="m", patch={"x": True}, scope="all")


def _u(*parts) -> float:
    h = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def world(effect: float):
    """Independent Bernoulli arms around 0.5, the mutation adds `effect`."""

    def run(t, c):
        on = bool(c.get("x"))
        return float(_u(t.id, on) < 0.5 + (effect if on else 0.0))

    return run


def tasks(prefix: str, n: int, tagged: bool = False) -> list[Task]:
    return [
        Task(
            id=f"{prefix}-{i}",
            tags={"type": "ab"[i % 2], "dom": "xy"[(i // 2) % 2]} if tagged else {},
            data={},
        )
        for i in range(n)
    ]


def verdicts(effect: float, seeds: int, n: int = 100, probe: bool = False) -> Counter:
    c: Counter = Counter()
    for s in range(seeds):
        g = Gate(
            run=world(effect),
            dev=tasks(f"d{s}", n, tagged=probe),
            probe=tasks(f"p{s}", n, tagged=True) if probe else None,
            draws=1,
            seed=s,
        )
        c[g.admit({}, M).verdict] += 1
    return c


def test_null_admits_decide_at_or_under_nominal():
    """Exact McNemar holds ~4% here; the paired z-test it replaced on binary
    scores drifted to ~6.5% at the same nominal 5%."""
    c = verdicts(0.0, seeds=1000)
    decided = (c[Verdict.COMMIT] + c[Verdict.REJECT]) / 1000
    assert decided <= 0.055, c


def test_real_gain_has_power():
    c = verdicts(0.2, seeds=150)
    assert c[Verdict.COMMIT] / 150 >= 0.70, c
    assert c[Verdict.REJECT] == 0, c


def test_uniform_gain_is_rarely_narrowed():
    """A +0.2 gain on every item must not come back scope_correct just
    because noise made one tag split look flat outside (was 22%)."""
    c = verdicts(0.2, seeds=300, n=48, probe=True)
    assert c[Verdict.SCOPE_CORRECT] / 300 <= 0.06, c


@pytest.mark.parametrize("effect,ceiling", [(0.0, 0.03), (0.05, 0.02)])
def test_reaudit_keeps_healthy_changes(effect, ceiling):
    """Re-audit revoked a no-op 31% and a +0.05 change 15% under the old
    rule. Revocation now needs significant harm."""
    revoked = 0
    for s in range(400):
        g = Gate(run=world(effect), dev=tasks(f"a{s}", 48), draws=1, seed=s)
        g._admit_committed(M)
        revoked += g.audit({})[0].detail["status"] == "revoked"
    assert revoked / 400 <= ceiling


def test_reaudit_still_revokes_real_harm():
    g = Gate(run=world(-0.3), dev=tasks("h", 48), draws=1)
    g._admit_committed(M)
    assert g.audit({})[0].detail["status"] == "revoked"


def test_duplicate_ids_refused():
    t = Task(id="same", tags={}, data={})
    with pytest.raises(ValueError, match="duplicate task ids"):
        Gate(run=world(0.0), dev=[t] * 48)


def test_shared_dev_probe_ids_refused():
    dev = tasks("x", 48)
    with pytest.raises(ValueError, match="dev and probe decks share 48"):
        Gate(run=world(0.0), dev=dev, probe=dev)
    with pytest.raises(ValueError, match="dev and reserve"):
        Gate(run=world(0.0), dev=dev, reserve=dev[:3])


def test_dev_memorizer_fails_transfer():
    """A change that only helps items it has seen scores +1.0 on dev and
    nothing on fresh items. Disjoint decks alone don't stop it, a pure
    no-harm probe would pass a 0.000 probe diff; the transfer test must."""
    dev = tasks("dev", 48)
    seen = {t.id for t in dev}
    g = Gate(
        run=lambda t, c: float(bool(c.get("x")) and t.id in seen),
        dev=dev,
        probe=tasks("fresh", 48),
        draws=1,
    )
    d = g.admit({}, M)
    assert d.verdict == Verdict.REJECT
    assert d.detail["probe"]["reason"] == "no_transfer"


def test_real_gain_passes_transfer():
    g = Gate(run=world(0.3), dev=tasks("dev", 100), probe=tasks("fresh", 100), draws=1)
    assert g.admit({}, M).verdict == Verdict.COMMIT


def test_generator_duplicate_ids_dropped():
    dev = tasks("dev", 16)

    def gen(region, n, rng):
        # repeats a dev id and its own id; only one fresh item is new
        return [dev[0], Task(id="new", tags={}, data={}), Task(id="new", tags={}, data={})]

    g = Gate(run=world(0.0), dev=dev, generate=gen, draws=1)
    assert [t.id for t in g._draw_fresh(lambda t: True, 3, g._rng("g"))] == ["new"]


def test_verdict_carries_ci():
    g = Gate(run=world(0.3), dev=tasks("ci", 100), draws=1)
    d = g.admit({}, M)
    lo, hi = d.detail["ci95"]
    assert lo <= d.detail["diff"] <= hi and lo > 0
