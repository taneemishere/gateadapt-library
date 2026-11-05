"""RunSurface: adapts the user's run(task, config) -> score into paired,
draw-indexed, cached episodes.

Every measurement call picks one draw slot idx in [0, draws) and evaluates
BOTH arms at that slot — the analogue of a shared random seed per item.
Cache misses call run() exactly once per (task, config, slot) and are
reused across measurements; beyond `draws` slots the cache serves old
draws, so repeat measurements are free but stale — matching the honesty
contract that a slot is one episode.
"""

from __future__ import annotations

import hashlib
import json
import random
from typing import Any, Callable

from .cache import EpisodeCache
from .models import Task


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
        self.name = name or getattr(run, "__qualname__", "run")
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
            self.cache.append(key, float(self.run(task, config)))
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
