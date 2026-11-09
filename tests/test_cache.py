from gateadapt import Task
from gateadapt.cache import EpisodeCache
from gateadapt.surface import RunSurface


def test_slot_determinism():
    calls = []

    def run(task, cfg):
        calls.append(1)
        return len(calls) * 0.1  # different every call

    s = RunSurface(run, draws=3, name="t")
    task = Task(id="a")
    first = s.draw(task, {}, 0)
    assert s.draw(task, {}, 0) == first  # same slot -> cached
    assert len(calls) == 1
    second = s.draw(task, {}, 1)
    assert s.draw(task, {}, 1) == second
    assert len(calls) == 2
    # beyond draws budget -> reuses last slot, no new episode
    assert s.draw(task, {}, 7) == s.draw(task, {}, 2)
    assert len(calls) == 3


def test_config_keys_distinguish_arms():
    def run(task, cfg):
        return float(cfg.get("x", 0))

    s = RunSurface(run, draws=1)
    t = Task(id="a")
    assert s.draw(t, {"x": 1.0}, 0) == 1.0
    assert s.draw(t, {"x": 0.7}, 0) == 0.7


def test_persistence(tmp_path):
    path = tmp_path / "cache.json"

    def run(task, cfg):
        return 0.42

    s = RunSurface(run, draws=1, cache=EpisodeCache(path))
    s.draw(Task(id="a"), {}, 0)
    s.flush()
    c2 = EpisodeCache(path)
    s2 = RunSurface(run, draws=1, cache=c2)
    assert s2.draw(Task(id="a"), {}, 0) == 0.42
    assert s2.episodes == 0  # served from disk, no fresh run


def test_save_leaves_no_tmp(tmp_path):
    path = tmp_path / "episodes.json"
    c = EpisodeCache(path)
    c.append("k", 0.5)
    c.save()
    assert not (tmp_path / "episodes.json.tmp").exists()
    assert EpisodeCache(path)._data == {"k": [0.5]}


def test_interrupted_save_preserves_existing_file(tmp_path, monkeypatch):
    """A kill during the write hits the tmp file, the live file keeps the
    last complete save."""
    from pathlib import Path

    path = tmp_path / "episodes.json"
    c = EpisodeCache(path)
    c.append("k", 0.5)
    c.save()
    c.append("k2", 1.0)
    orig = Path.write_text

    def boom(self, *a, **k):
        if self.name.endswith(".tmp"):
            raise OSError("killed mid-write")
        return orig(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", boom)
    try:
        c.save()
        assert False, "save should have raised"
    except OSError:
        pass
    assert EpisodeCache(path)._data == {"k": [0.5]}
