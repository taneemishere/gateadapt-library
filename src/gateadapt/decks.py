"""Decks: the instruments of the honesty invariant.

dev  , evolving; decides admission.
probe, fresh-item reserve for the pre-COMMIT transfer check.
audit, frozen at declaration; carries every reported claim.
test , held out; the final report only.

Synthesized/fresh items may grow the dev deck only, the invariant is
executable, not a convention.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import InputError
from .models import DeckRole, Task
from .scope import evaluate


class HonestyViolation(RuntimeError):
    """Raised when anything tries to feed items to a frozen or held-out
    instrument. This is the honesty invariant made executable."""


def check_unique_ids(items: list[Task], label: str) -> None:
    """A repeated id is the same item counted twice: one favorable draw
    would read as many independent wins. Refuse it at the boundary."""
    seen: set[str] = set()
    dupes: list[str] = []
    for t in items:
        if t.id in seen and t.id not in dupes:
            dupes.append(t.id)
        seen.add(t.id)
    if dupes:
        shown = ", ".join(repr(d) for d in dupes[:5])
        more = f" (+{len(dupes) - 5} more)" if len(dupes) > 5 else ""
        raise InputError(f"{label}: duplicate task ids {shown}{more}")


def check_disjoint(decks: dict[str, list[Task]]) -> None:
    """Every instrument must hold different items. A probe or audit item
    that is also a dev item can't test transfer, it re-tests the dev gain."""
    named = [(k, {t.id for t in v}) for k, v in decks.items() if v]
    for i, (a, ids_a) in enumerate(named):
        for b, ids_b in named[i + 1 :]:
            shared = sorted(ids_a & ids_b)
            if shared:
                shown = ", ".join(repr(x) for x in shared[:5])
                more = f" (+{len(shared) - 5} more)" if len(shared) > 5 else ""
                raise InputError(
                    f"{a} and {b} decks share {len(shared)} task id(s): {shown}{more}"
                )


@dataclass
class Deck:
    role: DeckRole
    items: list[Task]

    def region_items(self, scope_ast: Any) -> list[Task]:
        return [t for t in self.items if evaluate(scope_ast, t.features)]

    def add(self, items: list[Task]) -> None:
        if self.role != DeckRole.DEV:
            raise HonestyViolation(f"{self.role.value} decks never grow")
        self.items.extend(items)


def load_tasks(path: str | Path, role: DeckRole | None = None) -> list[Task] | Deck:
    """Load a JSONL task file. Each line is {"id": ..., "tags": {...}, ...rest}.

    'id' (or 'task_id') is required. 'tags' is the scope feature dict;
    remaining top-level fields land in task.data for run() to consume.
    With `role`, returns a Deck at that role instead of a bare list.
    """
    tasks: list[Task] = []
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError as e:
                raise InputError(f"{path}:{ln} is not valid JSON ({e})") from e
            if not isinstance(obj, dict):
                raise InputError(f"{path}:{ln}: a task must be a JSON object")
            tid = obj.get("id") or obj.get("task_id")
            if not tid:
                raise InputError(f"{path}:{ln}: task has no 'id'")
            tags = dict(obj.get("tags") or obj.get("features") or {})
            data = {k: v for k, v in obj.items() if k not in ("id", "task_id", "tags", "features")}
            tasks.append(Task(id=str(tid), tags=tags, data=data))
    check_unique_ids(tasks, str(path))
    if role is not None:
        return Deck(role, tasks)
    return tasks


def dump_tasks(tasks: list[Task], path: str | Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for t in tasks:
            f.write(json.dumps({"id": t.id, "tags": t.tags, **t.data}) + "\n")
