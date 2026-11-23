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
    , encoding="utf-8")
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


def _err_case(tmp_path, name):
    """(argv, expected exit, expected stderr fragment) for one failure mode."""
    t = tmp_path
    run = str(EX / "toy_agent.py") + ":run"
    mut = str(EX / "mutations/refusal_contract.yaml")
    base = ["--config", str(EX / "config.yaml"), "--tasks", str(EX / "tasks_dev.jsonl")]
    files = {
        "malformed.yaml": "id: [broken\n",
        "bad_scope.yaml": 'id: bad\npatch: {x: 1}\nscope: "tags.type === nope"\n',
        "list.yaml": "- just\n- a list\n",
        "raising.py": "def run(task, config):\n    raise RuntimeError('boom')\n",
        "big.py": "def run(task, config):\n    return 7.0\n",
        "bad_import.py": "import not_a_real_module_xyz\ndef run(t, c):\n    return 0.0\n",
        "cache.json": "not json",
        "ledger.jsonl": '{"event":\n',
        "dup.jsonl": '{"id": "x"}\n{"id": "x"}\n',
        "badline.jsonl": '{"id": "x"}\nnot json\n',
    }
    for f, body in files.items():
        (t / f).write_text(body, encoding="utf-8")
    return {
        "malformed_yaml": (["admit", str(t / "malformed.yaml"), *base, "--run", run], 2, "line 2, column 1"),
        "bad_scope": (["admit", str(t / "bad_scope.yaml"), *base, "--run", run], 2, "cannot parse scope"),
        "not_a_mapping": (["admit", str(t / "list.yaml"), *base, "--run", run], 2, "expected a mapping"),
        "missing_mutation": (["admit", str(t / "nope.yaml"), *base, "--run", run], 2, "no such file"),
        "missing_run_file": (["admit", mut, *base, "--run", str(t / "nope.py") + ":run"], 2, "no such file"),
        "missing_run_attr": (["admit", mut, *base, "--run", str(EX / "toy_agent.py") + ":nope"], 2, "has no 'nope'"),
        "bad_run_spec": (["admit", mut, *base, "--run", "toy_agent"], 2, "file.py:function"),
        "run_raises": (["admit", mut, *base, "--run", str(t / "raising.py") + ":run"], 4, "raised RuntimeError"),
        "score_out_of_range": (["admit", mut, *base, "--run", str(t / "big.py") + ":run"], 4, "[0, 1]"),
        "agent_import_fails": (["admit", mut, *base, "--run", str(t / "bad_import.py") + ":run"], 4, "ModuleNotFoundError"),
        "corrupt_cache": (["admit", mut, *base, "--run", run, "--cache", str(t / "cache.json")], 2, "not a valid cache"),
        "corrupt_ledger": (["admit", mut, *base, "--run", run, "--ledger", str(t / "ledger.jsonl")], 2, "ledger.jsonl:1"),
        "duplicate_ids": (["admit", mut, "--config", str(EX / "config.yaml"), "--tasks", str(t / "dup.jsonl"), "--run", run], 2, "duplicate task ids"),
        "bad_task_line": (["admit", mut, "--config", str(EX / "config.yaml"), "--tasks", str(t / "badline.jsonl"), "--run", run], 2, "badline.jsonl:2"),
    }[name]


ERR_CASES = [
    "malformed_yaml", "bad_scope", "not_a_mapping", "missing_mutation", "missing_run_file",
    "missing_run_attr", "bad_run_spec", "run_raises", "score_out_of_range", "agent_import_fails",
    "corrupt_cache", "corrupt_ledger", "duplicate_ids", "bad_task_line",
]


import pytest  # noqa: E402


@pytest.mark.parametrize("name", ERR_CASES)
def test_errors_are_one_line_with_dedicated_exit(tmp_path, capsys, monkeypatch, name):
    """No traceback, never exit 1 (that is REJECT): 2 for bad input, 4 when
    the agent's run() fails."""
    monkeypatch.delenv("GATEADAPT_DEBUG", raising=False)
    argv, code, fragment = _err_case(tmp_path, name)
    assert main(argv) == code
    err = capsys.readouterr().err
    assert err.startswith("gateadapt: error: ") and fragment in err
    assert "Traceback" not in err and err.count("\n") == 1


def test_debug_env_reraises(tmp_path, monkeypatch):
    from gateadapt.errors import InputError

    monkeypatch.setenv("GATEADAPT_DEBUG", "1")
    argv, _, _ = _err_case(tmp_path, "bad_scope")
    with pytest.raises(InputError):
        main(argv)


def test_version_flag(capsys):
    import gateadapt

    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"gateadapt {gateadapt.__version__}"


def test_budget_exhausted_prints_reason(tmp_path, capsys):
    led = str(tmp_path / "l.jsonl")
    argv = ["admit", str(EX / "mutations/topk_boost.yaml"), *_toy("--ledger", led, "--looks", "2", "--wealth", "0.05")]
    for _ in range(3):
        main(argv)
    capsys.readouterr()
    assert main(argv) == 3
    assert "has spent its alpha budget" in capsys.readouterr().out


def test_synthesis_verdict_labels_fresh_pool(capsys):
    """A G2 verdict rests on fresh items; printing it as n/dev_n read as a
    share of the dev deck (the review saw "18/16 items")."""
    main(["admit", str(EX / "mutations/refusal_contract.yaml"), *_toy(),
          "--reserve", str(EX / "tasks_reserve.jsonl")])
    out = capsys.readouterr().out
    assert "fresh test pool: 24 items" in out and "region: 24/48" not in out


def test_init_copies_examples_and_refuses_overwrite(tmp_path, capsys):
    assert main(["init", str(tmp_path)]) == 0
    ex = tmp_path / "examples"
    assert (ex / "config.yaml").exists() and (ex / "toy_agent.py").exists()
    assert (ex / "mutations" / "refusal_contract.yaml").exists()
    assert not any(p.name.startswith("__") for p in ex.rglob("*"))
    # the copied quickstart runs as-is
    rc = main(["admit", str(ex / "mutations/always_answer.yaml"),
               "--config", str(ex / "config.yaml"), "--tasks", str(ex / "tasks_dev.jsonl"),
               "--run", str(ex / "toy_agent.py") + ":run"])
    assert rc == 1
    assert main(["init", str(tmp_path)]) == 2
    assert "--force" in capsys.readouterr().err
    assert main(["init", str(tmp_path), "--force"]) == 0


@pytest.mark.parametrize(
    "argv_fn,fragment",
    [
        (lambda t: ["status", "--ledger", str(t / "nope.jsonl")], "no such file"),
        (lambda t: ["status", "--ledger", str(t / "bad.jsonl")], "bad.jsonl:1"),
        (lambda t: ["audit", *["--config", str(EX / "config.yaml"), "--tasks", str(EX / "tasks_dev.jsonl"), "--run", str(EX / "toy_agent.py") + ":run"], "--ledger", str(t / "nope.jsonl")], "no such file"),
        (lambda t: ["audit", *["--config", str(EX / "config.yaml"), "--tasks", str(EX / "tasks_dev.jsonl"), "--run", str(EX / "toy_agent.py") + ":run"], "--ledger", str(t / "bad.jsonl")], "bad.jsonl:1"),
        (lambda t: ["power", "--effect", "0.1", "--tasks", str(t / "dup.jsonl")], "duplicate task ids"),
        (lambda t: ["power", "--effect", "0.1", "--tasks", str(EX / "tasks_dev.jsonl"),
                    "--scope", "tags.type === x"], "cannot parse scope"),
        (lambda t: ["sequence", str(EX / "mutations/topk_boost.yaml"), *_toy(),
                    "--code", str(t / "nope")], "--code"),
        (lambda t: ["replay", "--autoharness", str(t / "nope")], "not an AutoHarness clone"),
    ],
    ids=["status_missing", "status_corrupt", "audit_missing", "audit_corrupt",
         "power_dup_ids", "power_bad_scope", "sequence_bad_code", "replay_bad_root"],
)
def test_every_command_errors_cleanly(tmp_path, capsys, monkeypatch, argv_fn, fragment):
    monkeypatch.delenv("GATEADAPT_DEBUG", raising=False)
    (tmp_path / "bad.jsonl").write_text('{"event":\n', encoding="utf-8")
    (tmp_path / "dup.jsonl").write_text('{"id": "x"}\n{"id": "x"}\n', encoding="utf-8")
    assert main(argv_fn(tmp_path)) == 2
    err = capsys.readouterr().err
    assert err.startswith("gateadapt: error: ") and fragment in err and "Traceback" not in err


def test_code_flag_on_sequence_and_audit(tmp_path, capsys):
    """--code reaches the cache key on every command that measures."""
    helper = tmp_path / "harness"
    helper.mkdir()
    (helper / "policy.txt").write_text("0.0", encoding="utf-8")
    agent = tmp_path / "agent.py"
    agent.write_text(
        "import pathlib\n"
        "POLICY = pathlib.Path(__file__).parent / 'harness' / 'policy.txt'\n"
        "def run(task, config):\n"
        "    if config.get('contract') and task.tags.get('type') == 'refusal':\n"
        "        return 1.0 - float(POLICY.read_text(encoding='utf-8'))\n"
        "    return 0.0\n"
    , encoding="utf-8")
    led, cache = str(tmp_path / "l.jsonl"), str(tmp_path / "c.json")
    common = ["--config", str(EX / "config.yaml"), "--tasks", str(EX / "tasks_dev.jsonl"),
              "--run", f"{agent}:run", "--cache", cache, "--code", str(helper)]
    assert main(["sequence", str(EX / "mutations/refusal_contract.yaml"), *common, "--ledger", led]) == 0
    main(["audit", *common, "--ledger", led])
    assert "kept" in capsys.readouterr().out
    (helper / "policy.txt").write_text("1.0", encoding="utf-8")  # the committed change now does nothing
    assert main(["audit", *common, "--ledger", led]) == 0
    out = capsys.readouterr().out
    assert "diff +0.000" in out  # re-measured with the edited helper, not served from cache


def test_run_spec_with_windows_drive_letter(tmp_path):
    """A Windows path carries its own colon; the function name is whatever
    follows the last one."""
    from gateadapt.cli import _load_run
    from gateadapt.errors import InputError

    agent = tmp_path / "agent.py"
    agent.write_text("def run(task, config):\n    return 0.5\n", encoding="utf-8")
    assert _load_run(f"{agent}:run")(None, {}) == 0.5
    with pytest.raises(InputError, match="file.py:function"):
        _load_run("C:\\agent.py")  # rpartition finds the drive colon, no function
    with pytest.raises(InputError, match="file.py:function"):
        _load_run("toy_agent")


def test_cp1252_console_does_not_crash(tmp_path):
    """The verdict lines use α and ·; a cp1252 pipe (the Windows default)
    must degrade them, not crash after the measurement ran."""
    import os
    import subprocess
    import sys

    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    env.pop("PYTHONUTF8", None)
    src = str(Path(__file__).resolve().parent.parent / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    p = subprocess.run(
        [sys.executable, "-m", "gateadapt", "admit", str(EX / "mutations/refusal_contract.yaml"),
         *_toy("--reserve", str(EX / "tasks_reserve.jsonl"))],
        capture_output=True,
    )
    assert p.returncode == 0, p.stderr.decode("cp1252", "replace")
    assert b"synthesis round 2" in p.stdout
