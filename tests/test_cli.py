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
    raw config — a duplicate patch has zero marginal on the stack and must
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
    assert rc2 == 0 and "verdict: abstain" in out  # no marginal on the stack


def test_power_cli(capsys):
    rc = main(["power", "--effect", "0.10"])
    out = capsys.readouterr().out
    assert rc == 0 and "tasks needed" in out


def test_replay_cli(capsys):
    rc = main(["replay", "--model", "9b"])
    out = capsys.readouterr().out
    assert rc == 0 and "9b-r1" in out and "commit" in out
