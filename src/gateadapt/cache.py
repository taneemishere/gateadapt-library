"""Episode cache: run() results keyed by (run name, task fingerprint,
effective-config fingerprint). Each key holds a list of draw scores; the
draw index is a slot, the same (task, config) pair always returns the same
score for a slot, which is what makes paired comparisons deterministic on
cached data.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _fp(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


class EpisodeCache:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._data: dict[str, list[float]] = {}
        if self.path and self.path.exists():
            self._data = {
                k: [float(x) for x in v]
                for k, v in json.loads(self.path.read_text()).items()
            }

    def key(self, run_name: str, task_fp: str, config: dict) -> str:
        return f"{run_name}|{task_fp}|{_fp(config)}"

    def append(self, key: str, score: float) -> int:
        draws = self._data.setdefault(key, [])
        draws.append(float(score))
        return len(draws) - 1

    def __len__(self) -> int:
        return len(self._data)

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # write-then-rename so a kill mid-save can't truncate the live
            # file, the only corruptible moment is before the replace
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, sort_keys=True))
            tmp.replace(self.path)
