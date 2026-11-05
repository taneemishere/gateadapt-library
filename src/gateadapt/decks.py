"""Decks: the instruments of the honesty invariant.

dev   — evolving; decides admission.
probe — fresh-item reserve for the pre-COMMIT transfer check.
audit — frozen at declaration; carries every reported claim.
test  — held out; the final report only.

Synthesized/fresh items may grow the dev deck only — the invariant is
executable, not a convention.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import DeckRole, Task
from .scope import evaluate


class HonestyViolation(RuntimeError):
    """Raised when anything tries to feed items to a frozen or held-out
    instrument. This is the honesty invariant made executable."""


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
    """Load a JSONL task file. Each line: {"id": ..., "tags": {...}, ...rest}.

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
            obj = json.loads(line)
            tid = obj.get("id") or obj.get("task_id")
            if not tid:
                raise ValueError(f"{path}:{ln}: task has no 'id'")
            tags = dict(obj.get("tags") or obj.get("features") or {})
            data = {k: v for k, v in obj.items() if k not in ("id", "task_id", "tags", "features")}
            tasks.append(Task(id=str(tid), tags=tags, data=data))
    if role is not None:
        return Deck(role, tasks)
    return tasks


def dump_tasks(tasks: list[Task], path: str | Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for t in tasks:
            f.write(json.dumps({"id": t.id, "tags": t.tags, **t.data}) + "\n")
