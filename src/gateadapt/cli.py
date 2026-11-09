"""gateadapt CLI: admit / audit / power / replay.

Exit codes for `admit`: 0 COMMIT or SCOPE_CORRECT · 1 REJECT · 0 ABSTAIN
(abstain is neutral, evidence, not failure).
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from .decks import load_tasks
from .gate import Gate
from .ledger import read_ledger
from .models import Mutation, Verdict
from .replay import format_replay, replay
from .stats import tasks_needed


def _load_mapping(path: str) -> dict[str, Any]:
    p = Path(path)
    text = p.read_text()
    if p.suffix in (".yaml", ".yml"):
        import yaml

        return yaml.safe_load(text)
    return json.loads(text)


def _load_run(spec: str):
    """'path/to/file.py:func' or 'module.path:func'."""
    target, _, attr = spec.partition(":")
    if not attr:
        raise SystemExit(f"--run needs 'file.py:function' or 'module:function', got {spec!r}")
    if target.endswith(".py"):
        mod = importlib.util.spec_from_file_location("gateadapt_user_run", target)
        module = importlib.util.module_from_spec(mod)
        mod.loader.exec_module(module)  # type: ignore[union-attr]
    else:
        module = importlib.import_module(target)
    fn = module
    for part in attr.split("."):
        fn = getattr(fn, part)
    return fn


def _build_gate(args, tasks_role="dev") -> Gate:
    dev = load_tasks(args.tasks)
    probe = load_tasks(args.probe) if getattr(args, "probe", None) else None
    audit = load_tasks(args.audit) if getattr(args, "audit", None) else None
    reserve = load_tasks(args.reserve) if getattr(args, "reserve", None) else None
    generate = _load_run(args.generate) if getattr(args, "generate", None) else None
    run = _load_run(args.run) if getattr(args, "run", None) else None
    return Gate(
        run=run,
        dev=dev,
        probe=probe,
        audit=audit,
        reserve=reserve,
        generate=generate,
        draws=args.draws,
        alpha=args.alpha,
        wealth=getattr(args, "wealth", None),
        looks=getattr(args, "looks", 4),
        items_per_round=getattr(args, "items_per_round", 24),
        min_n=args.min_n,
        seed=args.seed,
        cache_path=getattr(args, "cache", None),
        ledger_path=getattr(args, "ledger", None),
        name=getattr(args, "name", None),
    )


def _print_verdict(d) -> None:
    det = d.detail
    print(f"verdict: {d.verdict.value}")
    if "region_n" in det:
        print(
            f"  region: {det['region_n']}/{det['dev_n']} items · "
            f"paired diff {det['diff']:+.3f} (se {det['diff_se']:.3f}, p {det['p']:.4f}) · "
            f"wins {det['wins']} losses {det['losses']}"
        )
    if "heterogeneity" in det:
        h = det["heterogeneity"]
        print(
            f"  dev scan: in {h['diff_in']:+.3f} (n {h['n_in']}) · "
            f"out {h['diff_out']:+.3f} (n {h['n_out']})"
        )
        print(f"  narrowed scope: {det['narrowed_scope']}")
    if "synthesis_rounds" in det:
        for r in det["synthesis_rounds"]:
            if "reason" in r:
                print(f"  synthesis round {r['k']}: {r['reason']}")
            else:
                print(
                    f"  synthesis round {r['k']}: drew {r['items_drawn']} fresh · "
                    f"test pool {r['test_n']} · diff {r['diff']:+.3f} (p {r['p']:.4f} @ α {r['alpha']:.3f}) · {r['verdict']}"
                )
    if det.get("needed", {}).get("need"):
        n = det["needed"]
        print(f"  underpowered: have {n['have']} in-scope · need ~{n['need']} more (at ±{n['at_effect']:.2f})")
    if "g0_violations" in det:
        for v in det["g0_violations"]:
            print(f"  g0: {v}")
    if "caught_by" in det:
        print(f"  caught by: {det['caught_by']}")
    if "transfer_probe" in det:
        print(f"  transfer probe: {det['transfer_probe']}")
    for ev in det.get("lifecycle", []):
        if ev["event"] == "promote":
            print(
                f"  lifecycle: promoted deferred {ev['artifact_id']} "
                f"(conditional gain +{ev['gain']:.3f} with {ev['partner']})"
            )
        elif ev["event"] == "interaction":
            print(f"  lifecycle: interaction detected {ev['pair']} (gain {ev['gain']:+.3f})")
        elif ev["event"] == "revoke":
            print(f"  lifecycle: revoked {ev['artifact_id']} ({ev['reason']})")


def cmd_admit(args) -> int:
    cfg = _load_mapping(args.config)
    mut = Mutation.from_dict(_load_mapping(args.mutation))
    gate = _build_gate(args)
    d = gate.admit(cfg, mut)
    _print_verdict(d)
    return 0 if d.verdict in (Verdict.COMMIT, Verdict.SCOPE_CORRECT, Verdict.ABSTAIN_WITH_RECORD) else 1


def cmd_audit(args) -> int:
    cfg = _load_mapping(args.config)
    gate = _build_gate(args)
    committed = gate.committed
    if not committed:
        print("nothing committed (ledger empty or missing)")
        return 0
    print(f"re-auditing {len(committed)} committed mutation(s)")
    exit_code = 0
    for d in gate.audit(cfg):
        status = d.detail.get("status", "")
        print(
            f"  {d.artifact_id}: {status} · diff {d.detail.get('diff', 0.0):+.3f} "
            f"(n {d.detail.get('n', 0)})"
        )
        if status == "revoked":
            exit_code = 1
    rep = gate.audit_report(cfg)
    if rep.get("n"):
        print(f"  audit-deck read: diff {rep['diff']:+.3f} on {rep['n']} items (p {rep['p']:.4f})")
    return exit_code


def cmd_power(args) -> int:
    need = tasks_needed(
        effect=args.effect, base=args.base, alpha=args.alpha, power=args.power, stat=args.stat
    )
    print(f"paired tasks needed: {need} "
          f"(effect ±{args.effect}, base {args.base}, α {args.alpha}, power {args.power}, {args.stat}-test)")
    if args.tasks:
        tasks = load_tasks(args.tasks)
        if args.scope:
            from .models import Mutation

            m = Mutation(id="__probe__", patch={"x": 1}, scope=args.scope)
            region = [t for t in tasks if m.fires(t)]
        else:
            region = tasks
        have = len(region)
        print(f"  in scope: {have} → need ~{max(need - have, 0)} more" if have < need else f"  in scope: {have} → sufficient")
    return 0


def cmd_sequence(args) -> int:
    """Admit a stream of mutations through ONE persistent gate, the
    lifecycle story: deferred artifacts wait, promotions and revocations
    happen as later commits change the committed set."""
    cfg = _load_mapping(args.config)
    gate = _build_gate(args)
    paths = sorted(Path(p) for p in args.mutations)
    exit_code = 0
    for path in paths:
        mut = Mutation.from_dict(_load_mapping(str(path)))
        d = gate.admit(cfg, mut)
        print(f"{mut.id}:")
        _print_verdict(d)
        if d.verdict == Verdict.REJECT:
            exit_code = 1
    if gate.deferred:
        print("deferred:", ", ".join(f"{m.id} (retries {r})" for m, r in gate.deferred))
    return exit_code


def cmd_replay(args) -> int:
    models = ("9b", "27b") if args.model == "both" else (args.model,)
    for m in models:
        print(format_replay(replay(m, root=args.autoharness, min_n=args.min_n)))
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gateadapt", description="CI for agent changes")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("admit", help="G0+G1 admission check on one mutation")
    p.add_argument("mutation", help="mutation file (.yaml/.json): id, kind, patch, scope")
    p.add_argument("--config", required=True, help="base config (.yaml/.json)")
    p.add_argument("--tasks", required=True, help="dev tasks (.jsonl)")
    p.add_argument("--probe", help="probe tasks (.jsonl), pre-commit transfer check")
    p.add_argument("--audit", help="frozen audit tasks (.jsonl)")
    p.add_argument("--run", required=True, help="agent: 'file.py:function' or 'module:function'")
    p.add_argument("--reserve", help="consumable reserve tasks (.jsonl) for G2 synthesis rounds")
    p.add_argument("--generate", help="item synthesizer: 'file.py:function' or 'module:function'")
    p.add_argument("--wealth", type=float, help="family pool wealth (default: 2x alpha)")
    p.add_argument("--looks", type=int, default=4, help="looks per family pool (default 4)")
    p.add_argument("--items-per-round", type=int, default=24, help="fresh items per synthesis round")
    p.add_argument("--draws", type=int, default=3)
    p.add_argument("--alpha", type=float, default=0.05)
    p.add_argument("--min-n", type=int, default=8)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--cache", help="episode cache path (.json)")
    p.add_argument("--ledger", help="ledger path (.jsonl)")
    p.add_argument("--name", help="run-name for cache keys (bump when the agent changes)")
    p.set_defaults(fn=cmd_admit)

    p = sub.add_parser("sequence", help="admit a stream of mutations through one gate (G3 lifecycle)")
    p.add_argument("mutations", nargs="+", help="mutation files (.yaml/.json), processed in order")
    p.add_argument("--config", required=True)
    p.add_argument("--tasks", required=True)
    p.add_argument("--probe")
    p.add_argument("--audit")
    p.add_argument("--run", required=True)
    p.add_argument("--reserve")
    p.add_argument("--generate")
    p.add_argument("--wealth", type=float)
    p.add_argument("--looks", type=int, default=4)
    p.add_argument("--items-per-round", type=int, default=24)
    p.add_argument("--draws", type=int, default=3)
    p.add_argument("--alpha", type=float, default=0.05)
    p.add_argument("--min-n", type=int, default=8)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--cache")
    p.add_argument("--ledger")
    p.add_argument("--name")
    p.set_defaults(fn=cmd_sequence)

    p = sub.add_parser("audit", help="re-measure committed mutations; revoke harm")
    p.add_argument("--config", required=True)
    p.add_argument("--tasks", required=True, help="tasks to re-measure on (.jsonl)")
    p.add_argument("--audit", help="frozen audit tasks (.jsonl) for the final read")
    p.add_argument("--run", required=True)
    p.add_argument("--ledger", help="ledger with committed artifacts (.jsonl)")
    p.add_argument("--draws", type=int, default=3)
    p.add_argument("--alpha", type=float, default=0.05)
    p.add_argument("--min-n", type=int, default=8)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--cache")
    p.add_argument("--name")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("power", help="how many in-scope tasks a verdict needs")
    p.add_argument("--effect", type=float, required=True, help="effect size, e.g. 0.10")
    p.add_argument("--base", type=float, default=0.5, help="base solve rate")
    p.add_argument("--alpha", type=float, default=0.05)
    p.add_argument("--power", type=float, default=0.8)
    p.add_argument("--stat", choices=["z", "sign"], default="z")
    p.add_argument("--tasks", help="tasks (.jsonl) to compare have-vs-need")
    p.add_argument("--scope", help="scope DSL to count the in-scope region")
    p.set_defaults(fn=cmd_power)

    p = sub.add_parser("replay", help="replay the AutoHarness shipped history (zero compute)")
    p.add_argument("--model", choices=["9b", "27b", "both"], default="9b")
    p.add_argument("--autoharness", help="path to a local AutoHarness clone (default: bundled)")
    p.add_argument("--min-n", type=int, default=8)
    p.set_defaults(fn=cmd_replay)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
