#!/usr/bin/env python3
"""Extract the AutoHarness shipped-history panels into a bundled fixture.

Reads results/{model}_round{r}_dev/summary.json panels (dev20), the noise
panels, and the held-out test panels from a local AutoHarness clone, and
writes src/gateadapt/data/autoharness_{model}.json — enough for
`gateadapt replay` to run with zero compute and no clone.

Usage: python tools/make_replay_fixture.py /path/to/AutoHarness
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def panel(path: Path) -> dict[str, bool]:
    d = json.loads(path.read_text())
    return {t["task_id"]: bool(t["passed"]) for t in d["tasks"]}


def extract(root: Path, model: str) -> dict:
    res = root / "results"
    dev_ids = (res / "dev20_task_ids.txt").read_text().split()

    rounds = {}
    r = 0
    while (res / f"{model}_round{r}_dev" / "summary.json").exists():
        rounds[r] = panel(res / f"{model}_round{r}_dev" / "summary.json")
        r += 1

    edits = []
    for line in (root / "optimizer" / f"history_{model}.md").read_text().splitlines():
        if line.startswith("### Round "):
            parts = line.split("—")
            edits.append(
                {
                    "round": int(parts[0].split()[-1]),
                    "their": "KEPT" if "KEPT" in parts[-1] else "REJECTED",
                    "text": parts[-1].strip()[:400],
                }
            )

    out = {
        "model": model,
        "dev_ids": dev_ids,
        "panels": {str(k): v for k, v in rounds.items()},
        "edits": edits,
    }
    noise = res / f"{model}_noise_auto_dev" / "summary.json"
    if noise.exists():
        out["noise"] = panel(noise)
    for name in ("test_react", "test_auto"):
        p = res / f"{model}_{name}" / "summary.json"
        if p.exists():
            out[name] = panel(p)
    return out


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "../AutoHarness").resolve()
    out_dir = Path(__file__).resolve().parent.parent / "src" / "gateadapt" / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    for model in ("9b", "27b"):
        fx = extract(root, model)
        path = out_dir / f"autoharness_{model}.json"
        path.write_text(json.dumps(fx))
        print(f"{path.name}: {len(fx['dev_ids'])} dev ids, "
              f"{len(fx['panels'])} panels, {len(fx['edits'])} edits, "
              f"noise={'noise' in fx}, test={'test_react' in fx}")


if __name__ == "__main__":
    main()
