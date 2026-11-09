"""ledger.jsonl: one append-only line per gate event, verdicts, re-audits,
revocations. The ledger is the public record: every committed artifact
carries its evidence, and a revoked artifact stays visible (revocation is
an event, not a deletion).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .models import Mutation


def append_event(path: str | Path, event: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    event = {"ts": round(time.time(), 3), **event}
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=False) + "\n")


def read_ledger(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def committed_from_ledger(path: str | Path) -> list[Mutation]:
    """Rebuild the live committed set: COMMIT/SCOPE_CORRECT artifacts that
    were never revoked. Only serializable artifacts rebuild, callable
    scopes/apply can't round-trip a file."""
    committed: dict[str, Mutation] = {}
    order: list[str] = []
    for ev in read_ledger(path):
        kind = ev.get("event")
        if kind == "verdict":
            verdict = ev.get("verdict")
            art = ev.get("artifact") or {}
            if verdict in ("commit", "scope_correct") and art.get("serializable", True):
                m = Mutation.from_dict(art)
                if verdict == "scope_correct":
                    narrowed = (ev.get("detail") or {}).get("narrowed_scope")
                    if narrowed:
                        from .scope import from_jsonable

                        m = m.with_scope(from_jsonable(narrowed))
                if m.id not in committed:
                    order.append(m.id)
                committed[m.id] = m
            # a reject verdict never un-commits: it judged a candidate, not the
            # committed set. Only an explicit revoke event removes an artifact
        elif kind == "revoke":
            committed.pop(ev.get("artifact_id", ""), None)
    return [committed[i] for i in order if i in committed]


def pools_from_ledger(path: str | Path) -> dict[str, dict[str, Any]]:
    """Rebuild per-family pool state (spent wealth, look count) from the
    ledger's look_spend/look_refund events, so multiplicity control carries
    across processes and CI runs, the same way the committed set does."""
    pools: dict[str, dict[str, Any]] = {}
    for ev in read_ledger(path):
        kind = ev.get("event")
        if kind not in ("look_spend", "look_refund"):
            continue
        st = pools.setdefault(ev["family"], {"spent": 0.0, "looks": 0})
        if kind == "look_spend":
            st["spent"] += float(ev["alpha"])
            st["looks"] += 1
        else:
            st["spent"] = max(0.0, st["spent"] - float(ev["alpha"]))
    return pools
