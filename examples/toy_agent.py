"""A deterministic stand-in for a real agent: run(task, config) -> score.

Each task has a base solve prob `p` in its data. Config keys move it:

  contract: true               +0.30 on tags.type == "refusal"
  always_answer: true          -0.60 on refusal (answers what it should refuse)
  retrieval.top_k >= 8         +0.10 on type == "answer" (small lift)
  prune: true                  +0.55 on domain == "acme", -0.01 elsewhere
  lift: true                   +0.14 on type == "answer" (weak on dev-scale,
                               resolves via synthesis reserve)
  memoize_dev: true            +0.35 on seen (non-fresh) items, -0.30 on fresh
  needs_pair.b: true           +0.30 everywhere (the interaction partner)
  needs_pair.a: true           inert alone, +0.35 more when .b is committed
  conflict.a / conflict.b      +0.30 each alone; together they cannibalize
                               on fresh items (joint = solo - 0.30)

Score is a Bernoulli draw keyed on sha1(task.id | slot) — fully
deterministic, zero compute. The uniform is shared across configs at the
same slot, so paired arms share the random seed and only the mutation's
effect differs — the honest paired-measurement design.
"""

from __future__ import annotations

import hashlib
import json

_CALLS: dict[str, int] = {}


def _u(task_id: str, slot: int) -> float:
    h = hashlib.sha1(f"{task_id}|{slot}".encode()).digest()
    return int.from_bytes(h[:4], "big") / 2**32


def run(task, config: dict) -> float:
    cfg_fp = hashlib.sha1(json.dumps(config, sort_keys=True).encode()).hexdigest()[:12]
    key = f"{task.id}|{cfg_fp}"
    slot = _CALLS.get(key, 0)
    _CALLS[key] = slot + 1

    p = float(task.data.get("p", 0.5))
    t = task.tags
    if config.get("contract"):
        p += 0.30 if t.get("type") == "refusal" else 0.0
    if config.get("always_answer"):
        p += -0.60 if t.get("type") == "refusal" else 0.0
    if config.get("retrieval", {}).get("top_k", 4) >= 8:
        p += 0.10 if t.get("type") == "answer" else 0.0
    if config.get("prune"):
        p += 0.55 if t.get("domain") == "acme" else -0.01
    if config.get("lift"):
        p += 0.14 if t.get("type") == "answer" else 0.0
    if config.get("memoize_dev"):
        p += -0.30 if t.get("fresh") else 0.35
    np = config.get("needs_pair", {})
    if np.get("b"):
        p += 0.30
    if np.get("a") and np.get("b"):
        p += 0.35
    cf = config.get("conflict", {})
    if cf.get("a"):
        p += 0.30
    if cf.get("b"):
        p += 0.30
    if cf.get("a") and cf.get("b") and t.get("fresh"):
        p += -0.30

    p = min(max(p, 0.02), 0.98)
    return 1.0 if _u(task.id, slot) < p else 0.0


def generate(region_fn, n, rng):
    """Item synthesizer for --generate: draws fresh tasks whose tags satisfy
    the region predicate. A real deployment would call its task forge here —
    for the demo we mint deterministic synthetic tasks."""
    from gateadapt import Task

    out, i = [], 0
    while len(out) < n and i < n * 20:
        i += 1
        ttype = "answer" if i % 2 == 0 else "refusal"
        domain = "acme" if i % 5 < 2 else "nimbus"
        t = Task(
            id=f"syn-{rng.randrange(10**6)}",
            tags={"type": ttype, "domain": domain, "difficulty": "hard" if i % 4 == 0 else "easy"},
            data={"p": round(0.25 + rng.random() * 0.5, 3), "question": "synthesized"},
        )
        if region_fn(t):
            out.append(t)
    return out
