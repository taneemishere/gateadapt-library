"""RunSurface: adapts the user's run(task, config) -> score into paired,
draw-indexed, cached episodes.

Every measurement call picks one draw slot idx in [0, draws) and evaluates
BOTH arms at that slot, the analogue of a shared random seed per item.
Cache misses call run() exactly once per (task, config, slot) and are
reused across measurements; beyond `draws` slots the cache serves old
draws, so repeat measurements are free but stale, matching the honesty
contract that a slot is one episode.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .cache import EpisodeCache
from .errors import AgentError
from .models import Task

_LIB_DIR = Path(__file__).resolve().parent


def code_fingerprint(paths: list[str | Path]) -> str:
    """Short hash of the bytes of every file under `paths` (files or
    directories, walked in sorted order, caches and dotfiles skipped)."""
    h = hashlib.sha1()
    for root in paths:
        root = Path(root)
        files = [root] if root.is_file() else sorted(
            p for p in root.rglob("*")
            if p.is_file()
            and "__pycache__" not in p.parts
            and not any(part.startswith(".") for part in p.relative_to(root).parts)
        )
        for f in files:
            h.update(str(f.relative_to(root.parent) if root.is_dir() else f.name).encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


def default_run_name(run: Callable[..., Any]) -> str:
    """`run`'s qualname plus a hash of the file that defines it, so editing
    the agent can't serve episodes cached from the old code. gateadapt's
    own files are never hashed: a library upgrade must not throw away paid
    live episodes. Callables without a readable source keep the bare name."""
    name = getattr(run, "__qualname__", "run")
    try:
        src = Path(inspect.getsourcefile(run) or "").resolve()
    except (TypeError, OSError):
        return name
    if not src.is_file() or _LIB_DIR in src.parents:
        return name
    return f"{name}#{code_fingerprint([src])}"


class RunSurface:
    def __init__(
        self,
        run: Callable[[Task, dict], float],
        draws: int = 3,
        name: str | None = None,
        cache: EpisodeCache | None = None,
    ):
        self.run = run
        self.draws = max(1, int(draws))
        self.name = name or default_run_name(run)
        self.cache = cache if cache is not None else EpisodeCache()
        self.episodes = 0  # fresh run() calls this session

    def _config_fp(self, config: dict) -> str:
        blob = json.dumps(config, sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()[:16]

    def draw(self, task: Task, config: dict, idx: int) -> float:
        """Score for (task, config) at draw slot idx, cached."""
        key = self.cache.key(self.name, task.fingerprint, config)
        slot = min(idx, self.draws - 1)
        scores = self.cache._data.setdefault(key, [])
        while len(scores) <= slot:
            self.episodes += 1
            try:
                score = float(self.run(task, config))
            except Exception as e:  # noqa: BLE001, the user's agent failed
                # what was measured so far is real work, keep it
                self.flush()
                raise AgentError(
                    f"run() raised {type(e).__name__} on task {task.id!r}: {e}"
                ) from e
            if not 0.0 <= score <= 1.0:
                raise AgentError(
                    f"run() returned {score} on task {task.id!r}; scores must be in [0, 1]"
                )
            self.cache.append(key, score)
        return scores[slot]

    def pair(
        self, task: Task, base_cfg: dict, mut_cfg: dict, rng: random.Random
    ) -> tuple[float, float]:
        idx = rng.randrange(self.draws)
        base = self.draw(task, base_cfg, idx)
        mut = self.draw(task, mut_cfg, idx)
        return base, mut

    def flush(self) -> None:
        self.cache.save()
