import hashlib
import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))

from gateadapt import Task  # noqa: E402


def make_tasks(n=48, prefix="d", refusal_every=2, acme_every=5, fresh=False, seed=7):
    rng = random.Random(seed)
    tasks = []
    for i in range(n):
        tags = {
            "type": "refusal" if i % refusal_every == 0 else "answer",
            "domain": "acme" if i % acme_every < 2 else "nimbus",
            "difficulty": "hard" if i % 4 == 0 else "easy",
        }
        if fresh:
            tags["fresh"] = True
        tasks.append(
            Task(
                id=f"{prefix}-{i:03d}",
                tags=tags,
                data={"p": round(rng.uniform(0.25, 0.75), 3)},
            )
        )
    return tasks


def scripted_run(**effect_overrides):
    """Deterministic Bernoulli agent. Effects (defaults mirror the demo world):

      contract       +0.30 on type=="refusal"
      always_answer  -0.60 on refusal
      retrieval.top_k >= 8   +0.10 on type=="answer"
      prune          +0.55 on domain=="acme", -0.01 elsewhere
      memoize_dev    +0.35 non-fresh, -0.30 fresh
    """
    calls: dict[str, int] = {}
    fx = {
        "contract": (0.30, lambda t: t.tags.get("type") == "refusal"),
        "always_answer": (-0.60, lambda t: t.tags.get("type") == "refusal"),
        "prune_acme": (0.55, lambda t: t.tags.get("domain") == "acme"),
        "prune_else": (-0.01, lambda t: t.tags.get("domain") != "acme"),
        "memoize_fresh": (-0.30, lambda t: bool(t.tags.get("fresh"))),
        "memoize_seen": (0.35, lambda t: not t.tags.get("fresh")),
        "topk_answer": (0.10, lambda t: t.tags.get("type") == "answer"),
        "boost": (0.35, lambda t: True),
        # lifecycle world: needs_pair.b helps alone; .a only helps with .b
        "pair_b": (0.30, lambda t: True),
        "pair_ab": (0.35, lambda t: True),
        # conflict world: each flag helps alone; together they cannibalize —
        # but only on fresh items (joint = solo - 0.30), so dev can't see it
        # and marginals stay non-harmful: only the pairwise probe catches it
        "clash_solo": (0.30, lambda t: True),
        "clash_joint": (-0.30, lambda t: bool(t.tags.get("fresh"))),
    }
    fx.update(effect_overrides)

    def run(task, config):
        cfg_fp = hashlib.sha1(json.dumps(config, sort_keys=True).encode()).hexdigest()[:12]
        key = f"{task.id}|{cfg_fp}"
        slot = calls.get(key, 0)
        calls[key] = slot + 1
        p = float(task.data.get("p", 0.5))
        if config.get("contract"):
            p += fx["contract"][0] if fx["contract"][1](task) else 0
        if config.get("always_answer"):
            p += fx["always_answer"][0] if fx["always_answer"][1](task) else 0
        if config.get("retrieval", {}).get("top_k", 4) >= 8:
            p += fx["topk_answer"][0] if fx["topk_answer"][1](task) else 0
        if config.get("boost"):
            p += fx["boost"][0] if fx["boost"][1](task) else 0
        if config.get("prune"):
            p += fx["prune_acme"][0] if fx["prune_acme"][1](task) else fx["prune_else"][0]
        if config.get("memoize_dev"):
            p += fx["memoize_fresh"][0] if fx["memoize_fresh"][1](task) else fx["memoize_seen"][0]
        np = config.get("needs_pair", {})
        if np.get("b"):
            p += fx["pair_b"][0] if fx["pair_b"][1](task) else 0
        if np.get("a") and np.get("b"):
            p += fx["pair_ab"][0] if fx["pair_ab"][1](task) else 0
        cf = config.get("conflict", {})
        if cf.get("a"):
            p += fx["clash_solo"][0] if fx["clash_solo"][1](task) else 0
        if cf.get("b"):
            p += fx["clash_solo"][0] if fx["clash_solo"][1](task) else 0
        if cf.get("a") and cf.get("b"):
            p += fx["clash_joint"][0] if fx["clash_joint"][1](task) else 0
        p = min(max(p, 0.02), 0.98)
        # same task + same slot -> same uniform across configs: paired draws
        # share the random seed, so only the mutation's effect differs.
        h = hashlib.sha1(f"{task.id}|{slot}".encode()).digest()
        return 1.0 if int.from_bytes(h[:4], "big") / 2**32 < p else 0.0

    run.calls = calls
    return run


BASE_CFG = {"retrieval": {"top_k": 4, "min_score": 0.15}}
