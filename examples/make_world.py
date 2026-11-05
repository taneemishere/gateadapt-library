#!/usr/bin/env python3
"""Generate the example decks: tasks_dev.jsonl, tasks_probe.jsonl, tasks_audit.jsonl."""

from __future__ import annotations

import json
import random
from pathlib import Path

OUT = Path(__file__).parent


def gen(role: str, n: int, fresh: bool, seed: int) -> list[dict]:
    rng = random.Random(seed)
    tasks = []
    for i in range(n):
        ttype = "refusal" if i % 2 == 0 else "answer"
        domain = "acme" if i % 5 < 2 else "nimbus"
        tags = {"type": ttype, "domain": domain, "difficulty": "hard" if i % 4 == 0 else "easy"}
        if fresh:
            tags["fresh"] = True
        tasks.append(
            {
                "id": f"{role}-{i:03d}",
                "tags": tags,
                "p": round(rng.uniform(0.25, 0.75), 3),
                "question": f"synthetic task {role}-{i}",
            }
        )
    return tasks


def main() -> None:
    for name, tasks in [
        ("tasks_dev", gen("dev", 48, False, 11)),
        ("tasks_probe", gen("probe", 48, True, 23)),
        ("tasks_audit", gen("audit", 60, False, 37)),
        ("tasks_reserve", gen("res", 320, False, 53)),
    ]:
        path = OUT / f"{name}.jsonl"
        with open(path, "w") as f:
            for t in tasks:
                f.write(json.dumps(t) + "\n")
        print(f"wrote {path} ({len(tasks)} tasks)")


if __name__ == "__main__":
    main()
