import pytest

from gateadapt import Deck, DeckRole, HonestyViolation, Task, dump_tasks, load_tasks
from gateadapt.scope import parse


def test_load_tasks_roundtrip(tmp_path):
    tasks = [
        Task(id="a", tags={"type": "x"}, data={"q": "hello", "p": 0.5}),
        Task(id="b", tags={}, data={}),
    ]
    p = tmp_path / "t.jsonl"
    dump_tasks(tasks, p)
    loaded = load_tasks(p)
    assert loaded[0].id == "a" and loaded[0].tags == {"type": "x"}
    assert loaded[0].data["q"] == "hello" and loaded[0].data["p"] == 0.5
    assert loaded[1].tags == {}


def test_load_tasks_requires_id(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"tags": {}}\n')
    with pytest.raises(ValueError, match="no 'id'"):
        load_tasks(p)


def test_frozen_decks_reject_growth():
    t = Task(id="x")
    for role in (DeckRole.PROBE, DeckRole.AUDIT, DeckRole.TEST):
        d = Deck(role, [t])
        with pytest.raises(HonestyViolation):
            d.add([Task(id="y")])
    d = Deck(DeckRole.DEV, [t])
    d.add([Task(id="y")])
    assert len(d.items) == 2


def test_region_items():
    tasks = [Task(id="a", tags={"type": "refusal"}), Task(id="b", tags={"type": "answer"})]
    d = Deck(DeckRole.DEV, tasks)
    region = d.region_items(parse('tags.type == "refusal"'))
    assert [t.id for t in region] == ["a"]
