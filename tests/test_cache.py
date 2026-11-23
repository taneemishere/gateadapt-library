from pathlib import Path

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
        raise AssertionError("save should have raised")
    except OSError:
        pass
    assert EpisodeCache(path)._data == {"k": [0.5]}


def test_editing_the_agent_invalidates_its_cache(tmp_path):
    """The cache key carries a hash of the agent's code; an edited agent is
    measured fresh instead of being served the old code's scores."""
    from gateadapt.cli import main

    ex = Path(__file__).resolve().parent.parent / "examples"
    agent = tmp_path / "agent.py"
    argv = [
        "admit", str(ex / "mutations/always_answer.yaml"),
        "--config", str(ex / "config.yaml"), "--tasks", str(ex / "tasks_dev.jsonl"),
        "--run", f"{agent}:run", "--cache", str(tmp_path / "c.json"),
    ]
    agent.write_text("def run(task, config):\n    return 0.0\n", encoding="utf-8")
    assert main(argv) == 3  # inert: abstain
    agent.write_text("def run(task, config):\n    return 0.0 if config.get('always_answer') else 1.0\n", encoding="utf-8")
    assert main(argv) == 1  # the edit is seen: reject


def test_code_flag_hashes_helper_files(tmp_path):
    from gateadapt.cli import main

    ex = Path(__file__).resolve().parent.parent / "examples"
    helper = tmp_path / "harness"
    helper.mkdir()
    (helper / "policy.txt").write_text("0.0", encoding="utf-8")
    agent = tmp_path / "agent.py"
    agent.write_text(
        "import pathlib\n"
        "POLICY = pathlib.Path(__file__).parent / 'harness' / 'policy.txt'\n"
        "def run(task, config):\n"
        "    penalty = float(POLICY.read_text(encoding='utf-8'))\n"
        "    return 1.0 - penalty if config.get('always_answer') else 1.0\n", encoding="utf-8")
    argv = [
        "admit", str(ex / "mutations/always_answer.yaml"),
        "--config", str(ex / "config.yaml"), "--tasks", str(ex / "tasks_dev.jsonl"),
        "--run", f"{agent}:run", "--cache", str(tmp_path / "c.json"), "--code", str(helper),
    ]
    assert main(argv) == 3
    (helper / "policy.txt").write_text("1.0", encoding="utf-8")
    assert main(argv) == 1


def test_library_files_are_not_hashed():
    """A live_run closure is defined inside gateadapt; upgrading the library
    must not change its run name and throw away paid live episodes."""
    from gateadapt.live import exact_match, live_run
    from gateadapt.surface import default_run_name

    class C:
        model = "m"

        def chat(self, messages, **kw):
            return ""

    run = live_run(C(), exact_match())
    assert "#" not in default_run_name(run)


def test_code_fingerprint_tracks_content(tmp_path):
    from gateadapt.surface import code_fingerprint

    d = tmp_path / "pkg"
    d.mkdir()
    (d / "a.py").write_text("x = 1\n", encoding="utf-8")
    first = code_fingerprint([d])
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "a.pyc").write_bytes(b"junk")
    assert code_fingerprint([d]) == first  # caches don't count
    (d / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert code_fingerprint([d]) != first
