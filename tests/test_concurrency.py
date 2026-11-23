"""Concurrent admits on one ledger must not overspend a family's budget.

Real OS processes, released together, each holding a Gate that loaded the
ledger before the race started. Without the ledger lock every process
spends against its stale startup view (the review saw 0.15 of 0.10).
"""

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

WORKER = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, sys.argv[4])
    from gateadapt import Gate, Mutation, Task
    ledger, i, t0 = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
    tasks = [Task(id=str(k), tags={}, data={}) for k in range(48)]
    g = Gate(run=lambda t, c: 0.0, dev=tasks, ledger_path=ledger)
    time.sleep(max(0.0, t0 - time.time()))
    d = g.admit({}, Mutation(id=f"m{i}", family="shared", patch={"x": i}, scope="all"))
    print(d.detail.get("reason"))
    """
)


def _race(tmp_path, n, prespent=None):
    ledger = tmp_path / "ledger.jsonl"
    if prespent is not None:
        ledger.write_text(json.dumps({"event": "look_spend", "family": "shared", "alpha": prespent}) + "\n", encoding="utf-8")
    script = tmp_path / "worker.py"
    script.write_text(WORKER, encoding="utf-8")
    t0 = time.time() + 2.0
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(ledger), str(i), str(t0), str(SRC)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        for i in range(n)
    ]
    outs = [p.communicate(timeout=60) for p in procs]
    assert all(p.returncode == 0 for p in procs), [e for _, e in outs]
    reasons = [o.strip() for o, _ in outs]

    from gateadapt.ledger import pools_from_ledger

    return reasons, pools_from_ledger(ledger)["shared"]


def test_last_share_goes_to_exactly_one_process(tmp_path):
    reasons, pool = _race(tmp_path, 2, prespent=0.05)
    assert sorted(reasons) == ["family_budget_exhausted", "inconclusive"]
    assert abs(pool["spent"] - 0.10) < 1e-9


def test_eight_racing_processes_never_overspend(tmp_path):
    # wealth 0.10 at 0.05 a look funds exactly two unresolved looks
    reasons, pool = _race(tmp_path, 8)
    assert reasons.count("inconclusive") == 2
    assert reasons.count("family_budget_exhausted") == 6
    assert pool["spent"] <= 0.10 + 1e-9 and pool["looks"] == 2
