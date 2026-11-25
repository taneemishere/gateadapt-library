"""ledger.jsonl: one append-only line per gate event, verdicts, re-audits,
revocations. The ledger is the public record: every committed artifact
carries its evidence, and a revoked artifact stays visible (revocation is
an event, not a deletion).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import InputError
from .models import Mutation


@contextmanager
def ledger_lock(path: str | Path) -> Iterator[None]:
    """Exclusive cross-process lock on a ledger (a sidecar `.lock` file).

    Holding it across read-then-append makes the pair a transaction, which
    is what keeps two concurrent admits from both spending the last share
    of a family's alpha budget. Not reentrant: inside the lock, write with
    `_append`, not `append_event`."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_name(p.name + ".lock"), "a+b") as fh:
        try:
            import fcntl
        except ImportError:  # Windows
            import msvcrt

            fh.seek(0)
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue  # LK_LOCK gives up after ~10s; keep waiting
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _append(path: str | Path, event: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    event = {"ts": round(time.time(), 3), **event}
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=False) + "\n")


def append_event(path: str | Path, event: dict[str, Any]) -> None:
    with ledger_lock(path):
        _append(path, event)


def read_ledger(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    events = []
    for ln, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError as e:
            # the ledger is the record; never guess past a damaged line
            raise InputError(f"ledger {p}:{ln} is not valid JSON ({e})") from e
    return events


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


def lifecycle_from_ledger(path: str | Path) -> dict[str, Any]:
    """Rebuild the G3 lifecycle sets the way committed_from_ledger rebuilds
    the committed set: promoted partners, recorded interactions, revocations,
    rejected ids, and artifacts still deferred awaiting a partner, with the
    retry count their deferred_retry events recorded."""
    out: dict[str, Any] = {
        "revoked": [],
        "promoted_via": {},
        "interactions": [],
        "rejected": set(),
        "deferred": [],
    }
    deferred: dict[str, list] = {}
    for ev in read_ledger(path):
        kind = ev.get("event")
        if kind == "verdict":
            art = ev.get("artifact") or {}
            aid = art.get("id", "")
            det = ev.get("detail") or {}
            verdict = ev.get("verdict")
            if verdict in ("commit", "scope_correct"):
                deferred.pop(aid, None)
                if det.get("via_deferred") and det.get("partner"):
                    out["promoted_via"][aid] = det["partner"]
            elif verdict == "reject":
                if aid:
                    out["rejected"].add(aid)
                deferred.pop(aid, None)
            elif verdict == "abstain_with_record":
                if (
                    aid
                    and art.get("serializable", True)
                    and det.get("reason") != "family_budget_exhausted"
                ):
                    deferred[aid] = [Mutation.from_dict(art), 0]
        elif kind == "revoke":
            aid = ev.get("artifact_id", "")
            if aid not in out["revoked"]:
                out["revoked"].append(aid)
            deferred.pop(aid, None)
        elif kind == "deferred_retry":
            entry = deferred.get(ev.get("artifact_id", ""))
            if entry is not None:
                entry[1] += 1
        elif kind == "interaction":
            pair = ev.get("pair")
            if pair and pair not in out["interactions"]:
                out["interactions"].append(pair)
    out["deferred"] = list(deferred.values())
    return out
