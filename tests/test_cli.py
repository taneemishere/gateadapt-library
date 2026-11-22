import json
from pathlib import Path

from gateadapt.cli import main

EX = Path(__file__).resolve().parent.parent / "examples"


def test_admit_cli_commit(tmp_path, capsys):
    rc = main(
        [
            "admit",
            str(EX / "mutations/refusal_contract.yaml"),
            "--config", str(EX / "config.yaml"),
            "--tasks", str(EX / "tasks_dev.jsonl"),
            "--probe", str(EX / "tasks_probe.jsonl"),
            "--run", str(EX / "toy_agent.py") + ":run",
            "--ledger", str(tmp_path / "l.jsonl"),
            "--seed", "1",
        ]
    )
    assert rc == 0
    assert "commit" in capsys.readouterr().out


def test_admit_cli_reject_exit_code(capsys):
    rc = main(
        [
            "admit",
            str(EX / "mutations/always_answer.yaml"),
            "--config", str(EX / "config.yaml"),
            "--tasks", str(EX / "tasks_dev.jsonl"),
            "--probe", str(EX / "tasks_probe.jsonl"),
            "--run", str(EX / "toy_agent.py") + ":run",
            "--seed", "1",
        ]
    )
    assert rc == 1


def test_admit_cli_stacked_base(tmp_path, capsys):
    """The second admit must measure against the committed stack, not the
    raw config, a duplicate patch has zero marginal on the stack and must
    not re-commit. (Live run caught this: base_mean read 0.0 where the
    committed contract should have scored ~0.67.)"""
    ledger = tmp_path / "l.jsonl"
    dup = tmp_path / "dup.yaml"
    dup.write_text(
        "id: contract-dup\nkind: contract_patch\n"
        "scope: 'tags.type == \"refusal\"'\npatch:\n  contract: true\n"
    )
    rc1 = main(
        [
            "admit", str(EX / "mutations/refusal_contract.yaml"),
            "--config", str(EX / "config.yaml"),
            "--tasks", str(EX / "tasks_dev.jsonl"),
            "--probe", str(EX / "tasks_probe.jsonl"),
            "--run", str(EX / "toy_agent.py") + ":run",
            "--ledger", str(ledger), "--seed", "1",
        ]
    )
    assert rc1 == 0 and "commit" in capsys.readouterr().out
    rc2 = main(
        [
            "admit", str(dup),
            "--config", str(EX / "config.yaml"),
            "--tasks", str(EX / "tasks_dev.jsonl"),
            "--probe", str(EX / "tasks_probe.jsonl"),
            "--run", str(EX / "toy_agent.py") + ":run",
            "--ledger", str(ledger), "--seed", "1",
        ]
    )
    out = capsys.readouterr().out
    assert rc2 == 3 and "verdict: abstain" in out  # no marginal on the stack


def test_power_cli(capsys):
    rc = main(["power", "--effect", "0.10"])
    out = capsys.readouterr().out
    assert rc == 0 and "tasks needed" in out


def test_replay_cli(capsys):
    rc = main(["replay", "--model", "9b"])
    out = capsys.readouterr().out
    assert rc == 0 and "9b-r1" in out and "abstain_with_record" in out


def _toy(*extra):
    return [
        "--config", str(EX / "config.yaml"),
        "--tasks", str(EX / "tasks_dev.jsonl"),
        "--probe", str(EX / "tasks_probe.jsonl"),
        "--run", str(EX / "toy_agent.py") + ":run",
        *extra,
    ]


def test_abstain_exits_3_unless_abstain_ok(tmp_path, capsys):
    mut = str(EX / "mutations/topk_boost.yaml")
    assert main(["admit", mut, *_toy("--ledger", str(tmp_path / "a.jsonl"))]) == 3
    assert "abstain" in capsys.readouterr().out
    assert main(["admit", mut, *_toy("--ledger", str(tmp_path / "b.jsonl"), "--abstain-ok")]) == 0


def test_verdict_prints_ci(tmp_path, capsys):
    main(["admit", str(EX / "mutations/always_answer.yaml"), *_toy()])
    assert "95% CI: [" in capsys.readouterr().out


def test_sequence_keeps_given_order(tmp_path, capsys):
    """Paths run as given. The old alphabetical sort would have run
    needs_pair_a first even though b was passed first."""
    rc = main(
        [
            "sequence",
            str(EX / "mutations/needs_pair_b.yaml"),
            str(EX / "mutations/needs_pair_a.yaml"),
            *_toy("--ledger", str(tmp_path / "s.jsonl")),
        ]
    )
    out = capsys.readouterr().out
    assert out.index("needs-pair-b:") < out.index("needs-pair-a:")
    assert rc in (0, 3)


def test_duplicate_task_ids_rejected(tmp_path):
    import pytest

    dup = tmp_path / "dup.jsonl"
    dup.write_text('{"id": "x"}\n{"id": "x"}\n')
    with pytest.raises(ValueError, match="duplicate task ids"):
        main(["admit", str(EX / "mutations/refusal_contract.yaml"), "--config", str(EX / "config.yaml"),
              "--tasks", str(dup), "--run", str(EX / "toy_agent.py") + ":run"])


def test_synthesis_verdict_labels_fresh_pool(capsys):
    """A G2 verdict rests on fresh items; printing it as n/dev_n read as a
    share of the dev deck (the review saw "18/16 items")."""
    main(["admit", str(EX / "mutations/refusal_contract.yaml"), *_toy(),
          "--reserve", str(EX / "tasks_reserve.jsonl")])
    out = capsys.readouterr().out
    assert "fresh test pool: 24 items" in out and "region: 24/48" not in out
