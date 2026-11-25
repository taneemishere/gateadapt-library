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

from .errors import InputError


def _fp(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


class EpisodeCache:
    """Episode scores, plus G2 generated batches: a generator call is paid
    work too, so a rerun replays the same items instead of minting (and
    paying for) new ones.

    On disk: {"episodes": {...}, "generated": {...}}. A flat file of
    episodes only (the pre-0.6 format) still loads."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._data: dict[str, list[float]] = {}
        self.generated: dict[str, list[dict[str, Any]]] = {}
        if self.path and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and set(raw) <= {"episodes", "generated"} and raw:
                    episodes, self.generated = raw.get("episodes", {}), raw.get("generated", {})
                else:
                    episodes = raw
                self._data = {k: [float(x) for x in v] for k, v in episodes.items()}
            except (ValueError, TypeError, AttributeError) as e:
                raise InputError(
                    f"episode cache {self.path} is not a valid cache ({e}); "
                    "move it aside to start a fresh one"
                ) from e

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
            blob = {"episodes": self._data, "generated": self.generated}
            tmp.write_text(json.dumps(blob, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
