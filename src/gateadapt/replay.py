"""Replay: run an optimizer's shipped history through the gate.

The AutoHarness loop kept or rejected each prompt edit on a 20-task dev set
by pass@1 + tie-breakers — a threshold rule, not a paired test. This replays
the same panels through the real Gate: every edit becomes a Mutation whose
patch sets {"round": r}, and the committed-set stacking reproduces "current
best harness" as the base arm automatically.

Zero compute: bundled per-task panels answer run() by lookup.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .gate import Gate
from .models import DeckRole, Mutation, Task
from .decks import Deck

_DATA = Path(__file__).parent / "data"


@dataclass
class ReplayRow:
    artifact: str
    theirs: str
    verdict: str
    diff: float
    n: int
    caught_by: str = ""


def load_fixture(model: str, root: str | Path | None = None) -> dict[str, Any]:
    """Load bundled panels, or extract from a live AutoHarness clone."""
    if root is not None:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))
        from make_replay_fixture import extract  # type: ignore

        return extract(Path(root), model)
    return json.loads((_DATA / f"autoharness_{model}.json").read_text())


def replay(model: str = "9b", root: str | Path | None = None, min_n: int = 8) -> dict[str, Any]:
    fx = load_fixture(model, root)
    panels: dict[str, dict[str, bool]] = fx["panels"]
    dev_ids: list[str] = fx["dev_ids"]

    tasks = [Task(id=t, tags={"app": t.split("_")[0]}, data={}) for t in dev_ids]

    def run(task: Task, config: dict) -> float:
        return float(panels[str(config["round"])][task.id])

    gate = Gate(run=run, dev=Deck(DeckRole.DEV, tasks), draws=1, min_n=min_n, name=f"ah-{model}")

    rows: list[ReplayRow] = []
    for e in fx["edits"]:
        r = e["round"]
        m = Mutation(
            id=f"{model}-r{r}",
            kind="prompt_patch",
            patch={"round": r},
            scope="all",
            payload={"desc": e["text"]},
        )
        d = gate.admit({"round": 0}, m)
        rows.append(
            ReplayRow(
                artifact=m.id,
                theirs=e["their"],
                verdict=d.verdict.value,
                diff=round(d.detail.get("diff", 0.0), 4),
                n=d.detail.get("region_n", 0),
                caught_by=d.detail.get("caught_by", ""),
            )
        )

    out: dict[str, Any] = {"model": model, "rows": rows, "dev_pass": _dev_rates(panels, dev_ids)}

    if fx.get("noise") and "0" in panels:
        noise = fx["noise"]
        diffs = [float(noise.get(t, False)) - float(panels["0"].get(t, False)) for t in dev_ids]
        out["noise"] = {
            "n": len(diffs),
            "diff": round(sum(diffs) / len(diffs), 4) if diffs else 0.0,
            "flips": sum(1 for d in diffs if d != 0),
        }
    if fx.get("test_react") and fx.get("test_auto"):
        # last committed round = highest committed patch
        kept = max((int(c.patch["round"]) for c in gate.committed), default=0)
        tr, ta = fx["test_react"], fx["test_auto"]
        out["test"] = {
            "react_pass": round(sum(tr.values()) / len(tr), 4),
            "auto_pass": round(sum(ta.values()) / len(ta), 4),
            "kept_round": kept,
        }
    return out


def _dev_rates(panels: dict[str, dict[str, bool]], dev_ids: list[str]) -> dict[str, float]:
    return {
        r: round(sum(panels[r].get(t, False) for t in dev_ids) / max(len(dev_ids), 1), 3)
        for r in sorted(panels, key=int)
    }


def format_replay(result: dict[str, Any]) -> str:
    rows = result["rows"]
    lines = [f"## AutoHarness replay — {result['model']} (dev20, paired same-item)"]
    lines.append(f"{'artifact':<12} {'theirs':<10} {'gate':<22} {'diff':>7} {'n':>4}")
    for r in rows:
        tag = f" ({r.caught_by})" if r.caught_by else ""
        lines.append(f"{r.artifact:<12} {r.theirs:<10} {r.verdict + tag:<22} {r.diff:>+7.2f} {r.n:>4}")
    if "noise" in result:
        nz = result["noise"]
        lines.append(f"noise floor: {nz['flips']}/{nz['n']} same-harness task flips (diff {nz['diff']:+.3f})")
    if "test" in result:
        t = result["test"]
        lines.append(
            f"test168: react {t['react_pass']:.3f} -> auto {t['auto_pass']:.3f} "
            f"(gate kept through round {t['kept_round']})"
        )
    return "\n".join(lines)
