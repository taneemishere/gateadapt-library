"""Pin the README demo table to real runs. The gate semantics these verdicts
reflect are the contract; if a code change moves one, either the README
updates or the change is wrong. Toy world only, no API key."""

from pathlib import Path

import pytest
import yaml

from conftest import BASE_CFG
from gateadapt import Gate, Mutation, Verdict
from gateadapt.decks import load_tasks

EX = Path(__file__).resolve().parent.parent / "examples"
pytestmark = pytest.mark.skipif(
    not EX.exists(), reason="examples/ not shipped in the sdist"
)
toy_agent = pytest.importorskip("toy_agent")


def _gate(reserve=False, items_per_round=24):
    return Gate(
        run=toy_agent.make_run(),
        dev=load_tasks(EX / "tasks_dev.jsonl"),
        probe=load_tasks(EX / "tasks_probe.jsonl"),
        reserve=load_tasks(EX / "tasks_reserve.jsonl") if reserve else None,
        items_per_round=items_per_round,
    )


def _admit(gate, name):
    mut = Mutation.from_dict(
        yaml.safe_load((EX / "mutations" / f"{name}.yaml").read_text())
    )
    return gate.admit(dict(BASE_CFG), mut)


def test_refusal_contract_abstains_without_reserve():
    d = _admit(_gate(), "refusal_contract")
    assert d.verdict == Verdict.ABSTAIN_WITH_RECORD


def test_refusal_contract_commits_with_reserve():
    d = _admit(_gate(reserve=True), "refusal_contract")
    assert d.verdict == Verdict.COMMIT


def test_always_answer_rejects():
    d = _admit(_gate(), "always_answer")
    assert d.verdict == Verdict.REJECT


def test_topk_boost_abstains():
    d = _admit(_gate(), "topk_boost")
    assert d.verdict == Verdict.ABSTAIN_WITH_RECORD


def test_prune_hits_scope_corrects():
    d = _admit(_gate(), "prune_hits")
    assert d.verdict == Verdict.SCOPE_CORRECT


def test_memoize_dev_rejects_via_probe():
    d = _admit(_gate(), "memoize_dev")
    assert d.verdict == Verdict.REJECT
    assert d.detail.get("caught_by") == "transfer_probe"


def test_answer_lift_commits_via_synthesis():
    """The lift is uniform across answer items, so no subregion beats the
    rest significantly and the commit keeps the declared scope."""
    d = _admit(_gate(reserve=True, items_per_round=96), "answer_lift")
    assert d.verdict == Verdict.COMMIT
    assert d.detail["synthesis_rounds"][-1]["verdict"] == "commit"


@pytest.mark.parametrize(
    "a,b,expected_a,expected_b",
    [
        ("needs_pair_a", "needs_pair_b", Verdict.ABSTAIN_WITH_RECORD, Verdict.COMMIT),
    ],
)
def test_pair_promotion(a, b, expected_a, expected_b):
    gate = _gate()
    assert _admit(gate, a).verdict == expected_a
    d2 = _admit(gate, b)
    assert d2.verdict == expected_b
    promoted = [e for e in d2.detail["lifecycle"] if e["event"] == "promote"]
    assert promoted and promoted[0]["artifact_id"] == "needs-pair-a"


def test_conflict_b_caught_by_transfer_probe():
    gate = _gate()
    assert _admit(gate, "conflict_a").verdict == Verdict.COMMIT
    d2 = _admit(gate, "conflict_b")
    assert d2.verdict == Verdict.REJECT
    assert d2.detail["probe"]["reason"] == "no_transfer"


TOY = [
    "--config", str(EX / "config.yaml"),
    "--tasks", str(EX / "tasks_dev.jsonl"),
    "--probe", str(EX / "tasks_probe.jsonl"),
    "--run", str(EX / "toy_agent.py") + ":run",
]
RESERVE = ["--reserve", str(EX / "tasks_reserve.jsonl")]
M = lambda n: str(EX / "mutations" / f"{n}.yaml")


@pytest.mark.parametrize(
    "argv",
    [
        ["admit", M("refusal_contract"), *TOY],
        ["admit", M("refusal_contract"), *TOY, *RESERVE],
        ["admit", M("answer_lift"), *TOY, *RESERVE, "--items-per-round", "96"],
        ["sequence", M("needs_pair_a"), M("needs_pair_b"), *TOY],
        ["sequence", M("conflict_a"), M("conflict_b"), *TOY],
    ],
    ids=["quickstart", "reserve", "answer_lift", "needs_pair", "conflict"],
)
def test_readme_output_blocks_match_cli(argv, capsys):
    """Each demo's printed output must appear verbatim, as one contiguous
    block, in the README. Inline `# ...` annotations are ignored."""
    from gateadapt.cli import main

    main(argv)
    out = capsys.readouterr().out.rstrip("\n").splitlines()
    readme = [
        ln.split("        #")[0].rstrip()
        for ln in (EX.parent / "README.md").read_text().splitlines()
    ]
    starts = [i for i, ln in enumerate(readme) if ln == out[0]]
    assert any(readme[i : i + len(out)] == out for i in starts), "\n".join(out)
