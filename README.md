# GateAdapt

**CI for agent changes.** You supply

1. `run(task, config) -> score`, your agent's eval on one task
2. a tasks file (JSONL) with tags, split into **dev**, **probe**, and
   **audit** decks
3. a **mutation** with `id`, `kind`, `patch`, and a deterministic **scope**

Every change declares where it claims to help (its scope). It is tested
only there, on paired runs (same task, same draw, before vs after). The
gate answers

- **COMMIT** if the change measurably helps in its declared scope and the
  gain carries over to fresh probe items
- **REJECT** if it harms, leaks held-out data, or is structurally invalid
- **ABSTAIN** when there is not enough evidence yet. The verdict reports
  how much more evidence is needed
- **SCOPE-CORRECTED COMMIT** when the effect is real but confined to a
  subregion. The gate admits the change on the narrower scope instead of
  crediting the whole claim

Committed changes are re-measured later and can be **revoked**. Every
verdict and revocation is appended to `ledger.jsonl`; the decision log is
part of the artifact.

Abstaining is a real outcome. The gate exists because whole-suite
pass-rate diffs are noisy (in the AutoHarness dev20 panel, 6 of 20 tasks
flipped on an *unchanged* harness), and certifying noise is worse than
admitting nothing.

**How it works.** The gate is a staged pipeline. G0 validates the
mutation and its scope before a single episode runs. G1 makes a paired
measurement on the declared region only, same task and same draw slot on
both arms, then a transfer probe on fresh items catches dev-only gains.
When the dev look can't decide, G2 spends a per-family alpha budget on
direction-blind fresh items until the cumulative pool resolves. After
every commit, G3 re-measures the committed set. Pairwise probes catch
mutations that cancel each other, deferred artifacts get promoted when
their partner arrives, and drift audits revoke what stopped working.

## Install

```bash
pip install gateadapt            # use it
pip install -e ".[dev]"          # hack on it (editable + pytest)
```

## 5-minute quickstart

The repo ships a deterministic toy agent (no API key, zero compute).

```bash
gateadapt admit examples/mutations/refusal_contract.yaml \
    --config examples/config.yaml \
    --tasks examples/tasks_dev.jsonl \
    --probe examples/tasks_probe.jsonl \
    --run examples/toy_agent.py:run \
    --ledger ledger.jsonl
```

```
verdict: abstain_with_record
  region: 24/48 items · paired diff +0.208 (se 0.085, p 0.0625) · wins 5 losses 0
  95% CI: [-0.009, +0.394]
  underpowered: have 24 in-scope · need ~1 more decisive pair, best case at ±0.21
```

The effect is real but the evidence is thin. Five wins and zero losses is
too few discordant pairs to certify at this alpha, so the gate asks for
more evidence instead of trusting a lucky z tail. The interval still
includes 0, and it always agrees with the exact test that decided. The
exit code is 3, which means unknown, not passed. Feed it a reserve deck
and the synthesis loop supplies the missing items.

```bash
gateadapt admit examples/mutations/refusal_contract.yaml \
    --config examples/config.yaml \
    --tasks examples/tasks_dev.jsonl \
    --probe examples/tasks_probe.jsonl \
    --reserve examples/tasks_reserve.jsonl \
    --run examples/toy_agent.py:run
```

```
verdict: commit
  fresh test pool: 24 items · paired diff +0.292 (se 0.095, p 0.0156) · wins 7 losses 0
  95% CI: [+0.053, +0.493]
  synthesis round 1: drew 24 fresh · test pool 12 · diff +0.250 (p 0.2500 @ α 0.025) · inconclusive
  synthesis round 2: drew 24 fresh · test pool 24 · diff +0.292 (p 0.0156 @ α 0.025) · commit
```

Try the other demo mutations; they cover the full verdict space.

| mutation | verdict | why |
|---|---|---|
| `refusal_contract.yaml` | `abstain_with_record`, then `commit` with `--reserve` | +0.21 on refusals, too few discordant pairs alone |
| `always_answer.yaml` | `reject` | −0.21 paired diff, 0 wins 10 losses |
| `topk_boost.yaml` | `abstain_with_record` | +0.04 on 24 items, underpowered |
| `prune_hits.yaml` | `scope_correct` | helps only `tags.domain == "acme"` |
| `memoize_dev.yaml` | `reject` | helps dev, harms fresh items, so the transfer probe catches it |
| `answer_lift.yaml` | `commit` (via G2) | underpowered on dev, synthesis resolves it |

`answer_lift` shows G2. The effect is too weak to certify at dev scale, so
the gate draws fresh items and the cumulative test pool resolves it. The
lift is uniform across answer items, so the gate does not narrow it.

```bash
gateadapt admit examples/mutations/answer_lift.yaml \
    --config examples/config.yaml \
    --tasks examples/tasks_dev.jsonl \
    --probe examples/tasks_probe.jsonl \
    --reserve examples/tasks_reserve.jsonl \
    --items-per-round 96 \
    --run examples/toy_agent.py:run
```

```
verdict: commit
  fresh test pool: 48 items · paired diff +0.146 (se 0.051, p 0.0156) · wins 7 losses 0
  95% CI: [+0.026, +0.257]
  synthesis round 1: drew 96 fresh · test pool 48 · diff +0.146 (p 0.0156 @ α 0.025) · commit
```

The verdict rests on the fresh test pool, so that is the measurement the
first line shows, not the dev look that triggered synthesis. No reserve deck?
Pass `--generate examples/toy_agent.py:generate`, a callable
`(region_fn, n, rng) -> [Task]` that mints in-region items. In a real
deployment this is where your task forge goes.

### The lifecycle story (G3)

Feed a *stream* of mutations through one persistent gate.

```bash
gateadapt sequence \
    examples/mutations/needs_pair_a.yaml \
    examples/mutations/needs_pair_b.yaml \
    --config examples/config.yaml --tasks examples/tasks_dev.jsonl \
    --probe examples/tasks_probe.jsonl --run examples/toy_agent.py:run
```

```
needs-pair-a:
verdict: abstain_with_record        # inert alone, so it is deferred, not dead
  region: 48/48 items · paired diff +0.000 (se 0.000, p 1.0000) · wins 0 losses 0
  95% CI: [-0.055, +0.055]
  underpowered: have 48 in-scope · need ~6 more decisive pairs, best case at ±0.05
needs-pair-b:
verdict: commit
  region: 48/48 items · paired diff +0.208 (se 0.059, p 0.0020) · wins 10 losses 0
  95% CI: [+0.080, +0.332]
  transfer probe: pass
  lifecycle: promoted deferred needs-pair-a (conditional gain +0.208 with needs-pair-b)
```

`sequence` runs the files in the order given, and its exit code reports
where each artifact ended. Here both end committed, so it exits 0.

The abstained artifact was not wrong. It was an interaction arriving
before its partner. When the partner commits, the gate measures it
*conditionally* and promotes it without spending a budgeted look. The
opposite case also exists.

```bash
gateadapt sequence examples/mutations/conflict_a.yaml examples/mutations/conflict_b.yaml \
    --config examples/config.yaml --tasks examples/tasks_dev.jsonl \
    --probe examples/tasks_probe.jsonl --run examples/toy_agent.py:run
```

```
conflict-a:
verdict: commit
  region: 48/48 items · paired diff +0.208 (se 0.059, p 0.0020) · wins 10 losses 0
  95% CI: [+0.080, +0.332]
  transfer probe: pass
conflict-b:
verdict: reject
  region: 48/48 items · paired diff +0.208 (se 0.059, p 0.0020) · wins 10 losses 0
  95% CI: [+0.080, +0.332]
  caught by: transfer_probe (no_transfer: probe diff +0.000 on 48 items)
```

On top of `conflict-a`, `conflict-b` still gains on dev, but fresh items
show nothing, so the transfer probe rejects it before it is committed.
When a cancelling pair does get committed (for example, admitted without
a probe deck), the G3 pairwise probe finds the joint below the additive
expectation and revokes both (`lifecycle: revoked conflict-a
(interaction_conflict)`).

### Live substrate (real LLM episodes)

The same gate drives a real model. `run(task, config)` is a contract, and
`gateadapt.live` turns any `.chat(messages)` client into one.
`gateadapt.llm` ships a stdlib OpenAI-compatible client with a hard
token-bucket rate limiter (default 36 rpm) and transient-retry. A
mutation's `patch` reaches the wire per call. Supported keys are
`system_prompt`, `prompt_suffix`, `temperature`, `max_tokens`, `seed`,
and `model`.

```bash
gateadapt admit examples/mutations/strict_answers.yaml \
    --config examples/config.yaml \
    --tasks examples/tasks_live_dev.jsonl \
    --probe examples/tasks_live_probe.jsonl \
    --run examples/live_agent.py:run \
    --draws 1 --ledger live_ledger.jsonl --cache live_episodes.json
```

```
verdict: abstain_with_record
  region: 8/16 items · paired diff +0.625 (se 0.183, p 0.0625) · wins 5 losses 0
  95% CI: [-0.027, +1.000]
  underpowered: have 8 in-scope · need ~1 more decisive pair, best case at ±0.62
```

That was a real run on nemotron-3-ultra-550b via NIM (key from
`NVIDIA_API_KEY` or `GATEADAPT_NVAPI_KEYFILE`), 16 API calls, 36 seconds
of wall time. The model's verbose "2 + 2 = 4" fails `exact_match("4")`.
The output contract fixes 5 of the 8 answer items, which is still too few
discordant pairs to commit. The episode cache keys each call by (task,
config) fingerprint, so a rerun measures nothing twice. You can interrupt
and resume freely; replaying this run from the cache took 0 seconds and
printed the same verdict.

Temperature 0 is not bitwise deterministic on a hosted endpoint. The
same mutation fixed 4 of 8 items on 2026-11-19 and 5 of 8 on 2026-11-21.
That is why the gate pairs both arms on the same items and asks for
discordant pairs, not a single lucky run.

Every row below is a real v0.5 run on 2026-11-21 with the same model,
temperature 0 and `--draws 1`, each mutation on a fresh ledger. Live runs
reached all four verdicts.

| live mutation | verdict | evidence |
|---|---|---|
| `strict_answers` | `abstain_with_record` | +0.625 on answers, 5 wins 0 losses, p=0.0625 |
| `refusal_contract_live` | `commit` | +1.000 on refusals, 8 wins 0 losses, p=0.0078, transfer probe pass |
| `single_word` | `commit` | +0.750 on answers, 6 wins 0 losses, p=0.031, transfer probe pass |
| `context_only` | `scope_correct` | declared `all`, narrowed to `tags.type == "refusal"` (inside +1.000 on 8, outside +0.000 on 8) |
| `refuse_if_unsure` | `scope_correct` | declared `all`, narrowed to `tags.type == "refusal"` the same way |
| `temp_tune` | `abstain_with_record` | +0.000 on 8 items, 0 wins 0 losses |
| `terse_check` | `abstain_with_record` | +0.000 on 8 items, 0 wins 0 losses |
| `live_unknown` | `abstain_with_record` | +0.000 on its own, the verbose baseline already fails every answer item |
| `brief_answer` | `commit` via **G2** | one round of 22 LLM-minted items, fresh test pool of 11 at +0.636, 7 wins 0 losses, p=0.0156 at α 0.025 |
| sequence, then `live_unknown` | `reject` | stacked on committed `single_word`, −0.750, 0 wins 6 losses, p=0.031 |

The sequence ran `refusal_contract_live`, `strict_answers`, `single_word`
and `live_unknown` in that order. It committed, deferred, committed and
rejected. When `single_word` committed, the deferred `strict_answers` was
re-measured against it and stayed deferred. `gateadapt audit` on the
probe deck then kept both commits, and `gateadapt status` rebuilt the same
states from the ledger.

The G3 conditional promotion also runs live. `normalize_numbers` is a
harness output stage that turns a one-word number reply ("Two") into
digits ("2"). On the model's verbose answers it does nothing, so it
abstains and waits on the deferred queue. `single_word_pair` makes the
model answer in one word, and once it commits, the deferred artifact is
re-measured against it and promoted.

```bash
gateadapt sequence \
    examples/mutations/normalize_numbers.yaml \
    examples/mutations/single_word_pair.yaml \
    --config examples/config.yaml \
    --tasks examples/tasks_live_pair_dev.jsonl \
    --probe examples/tasks_live_pair_probe.jsonl \
    --run examples/live_agent.py:run --draws 1
```

```
normalize_numbers:
verdict: abstain_with_record
  region: 16/16 items · paired diff +0.000 (se 0.000, p 1.0000) · wins 0 losses 0
  95% CI: [-0.154, +0.154]
  underpowered: have 16 in-scope · need ~6 more decisive pairs, best case at ±0.05
single_word_pair:
verdict: commit
  region: 16/16 items · paired diff +0.625 (se 0.125, p 0.0020) · wins 10 losses 0
  95% CI: [+0.239, +0.901]
  transfer probe: pass
  lifecycle: promoted deferred normalize_numbers (conditional gain +0.250 with single_word_pair)
```

The +0.250 is the four small-number items out of 16 on the probe deck,
the ones single-word mode writes as words. `gateadapt status` then
reports `normalize_numbers conditional partner=single_word_pair`, so if
`single_word_pair` is ever revoked, the orphan check revokes it too.
NIM throttled this account below its nominal 40 rpm during these runs;
`GATEADAPT_MAX_RPM` lowers the client's ceiling (12 was used here).

Temperature 0 is the live example's default; `GATEADAPT_LLM_TEMPERATURE`
overrides it.

Episode caches are namespaced by ambient client params. The chain is
`run.__qualname__` → `RunSurface.name` → cache key, and it covers model,
temperature, seed, and max_tokens. Switching `GATEADAPT_LLM_MODEL` or
temperature never serves stale episodes.

```python
from gateadapt.llm import NvidiaNIMClient        # or any .chat() client
from gateadapt.live import live_run, exact_match, llm_judge, llm_generate

llm = NvidiaNIMClient.from_env(max_rpm=36)      # hard-capped under the 40 rpm tier
run = live_run(llm, exact_match())              # or contains(), predicate(fn), llm_judge(llm)
gate = Gate(run=run, dev=..., probe=..., generate=llm_generate(llm, describe="..."))
```

Then replay AutoHarness's shipped optimization history (bundled panels,
zero compute).

```bash
gateadapt replay --model both
gateadapt power --effect 0.10          # how many in-scope tasks a verdict needs
gateadapt power --effect 0.10 --tasks examples/tasks_dev.jsonl --scope 'tags.type == "answer"'
gateadapt status --ledger ledger.jsonl # lifecycle state of every artifact in the log
```

## In your CI

`admit` exits 0 on commit or scope-correct, 1 on reject, and 3 on
abstain. The exit code is the gate. The recipe for a harness repo is to have your
PR workflow render the proposed harness edit as a mutation YAML (`patch:`
holds the diff's effective-config change) and then run the measurement.
The repo's `.github/workflows/gateadapt.yml` dogfoods exactly this
pattern on the toy agent. For a live workload it looks like this.

```yaml
name: gate
on:
  pull_request:
    paths: ["harness/**", "mutations/**"]    # your harness + mutation files

jobs:
  admit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }

      - uses: actions/cache@v4               # episodes are the spend; keep them
        with:
          path: episodes.json
          key: gateadapt-${{ github.sha }}
          restore-keys: gateadapt-

      - run: pip install gateadapt

      - name: Measure the harness change
        env:
          NVIDIA_API_KEY: ${{ secrets.NVIDIA_API_KEY }}
        run: |
          set +e
          gateadapt admit mutations/${{ github.head_ref }}.yaml \
            --config harness/config.yaml \
            --tasks eval/dev.jsonl \
            --probe eval/probe.jsonl \
            --reserve eval/reserve.jsonl \
            --run harness/agent.py:run \
            --draws 1 --ledger eval/ledger.jsonl --cache episodes.json
          rc=$?
          # abstain (3) is unknown: surface it without failing the build
          if [ $rc -eq 3 ]; then echo "::warning::gateadapt abstained, not enough evidence"; exit 0; fi
          exit $rc

      - name: Commit the ledger back
        if: always()
        run: |
          git config user.name "gateadapt[bot]"
          git config user.email "gateadapt[bot]@users.noreply.github.com"
          git add eval/ledger.jsonl
          git commit -m "gateadapt ledger ${{ github.sha }}" || true
          git push
```

Notes that matter in CI.

- **The episode cache is the budget.** Persist it between runs
  (`actions/cache` above) or every retry pays full price again. Caches
  are namespaced by task, config, and ambient client params (model,
  temperature, seed), so key rotation is safe.
- **The ledger is the record — commit it, don't cache it.** It is the
  source of truth for the committed set and each family's spent look
  budget; a cache eviction would silently reset both, and concurrent PR
  runs overwrite one another's cache entry. Committing it to the repo
  (the step above) keeps the record append-only and ordered.
- **`admit` is resumable.** Kill it mid-measure and cached episodes carry
  over; the next run measures only what is missing.
- **Stacking needs the ledger.** The committed set and pool budgets are
  loaded from `ledger.jsonl` on `admit`, so later mutations measure on
  top of earlier committed ones.
- **Abstain is unknown, not passed.** It exits 3, so a pipeline can tell
  it apart from a commit. The recipe above turns it into a warning; pass
  `--abstain-ok` to exit 0 instead. Wire `--reserve` for G2 synthesis if
  you want abstains to try resolving themselves before giving up.
- Exit semantics are `0` for commit / scope-correct, `1` for reject, `3`
  for abstain.
- **Fork PRs cannot push the ledger back.** `GITHUB_TOKEN` is read-only on
  forks. Run this recipe on same-repo branches, or have a maintainer run
  it for fork contributions.

## Python API

```python
from gateadapt import Gate, Mutation, load_tasks

gate = Gate(
    run=my_agent,                                # run(task, config) -> score in [0,1]
    dev=load_tasks("tasks_dev.jsonl"),           # decides admission; grows with synthesized items
    probe=load_tasks("tasks_probe.jsonl"),       # fresh items, pre-commit transfer check
    audit=load_tasks("tasks_audit.jsonl"),       # frozen; carries reported claims only
    reserve=load_tasks("tasks_reserve.jsonl"),   # consumable item pool for G2 synthesis
    # generate=my_forge,                         # OR a callable (region_fn, n, rng) -> [Task]
    draws=3, alpha=0.05, seed=0,
    looks=4, items_per_round=24,                 # G2: synthesis rounds, fresh items per round
    cache_path="episodes.json", ledger_path="ledger.jsonl",
)

v = gate.admit(config, Mutation(
    id="prune-weak-hits",
    kind="retrieval_rule",
    patch={"retrieval.min_score": 0.35},          # dotted keys deep-merge
    scope='tags.type == "refusal"',               # or callable: lambda t: ...
))

# re-measure committed mutations; revoke measured harm (ledger events)
gate.audit(config)

# committed set vs base, on the frozen audit deck
gate.audit_report(config)

# where an artifact sits in the lifecycle, one query instead of
# membership checks across the gate's collections
gate.artifact_state("prune-weak-hits")
# {"state": LifecycleState.COMMITTED}
gate.artifact_state("deferred-thing")
# {"state": LifecycleState.DEFERRED, "retries": 1, "max_retries": 4}
gate.artifact_states()   # every known artifact, same shape
```

Subsequent `admit` calls measure each candidate **stacked on the
committed harness**. The base arm is the current admitted state, so a
second commit must beat the first, not the unmutated origin.

`gateadapt status --ledger ledger.jsonl` prints the same lifecycle map
from the command line (`--json` for machines): every artifact the ledger
knows, its state, and the details (partner for conditional commits,
retry count for deferred). States are `committed`, `conditional`,
`deferred`, `expired`, `revoked`, `rejected`, `unknown` — live
membership wins over history, so a revoked-then-recommitted artifact
reads `committed`, and every `Decision.detail["state_after"]` carries
the post-verdict state.

## Inputs

**tasks (JSONL).** One object per line; `id` required, `tags` is the
scope feature surface, and everything else is passed through to `run` as
`task.data`.

```jsonl
{"id": "t-001", "tags": {"type": "refusal", "domain": "acme"}, "question": "...", "p": 0.52}
```

**mutation (YAML/JSON).**

```yaml
id: prune-weak-hits
kind: retrieval_rule          # prompt_patch | tool_schema_patch | retrieval_rule |
                              # budget_rule | contract_patch | routing_rule | memory_rule
patch:
  retrieval.min_score: 0.35   # dotted keys deep-merge into the effective config
scope: 'tags.type == "refusal"'
# family: rerank-motif        # optional; declares shared alpha budget.
                              # Without it each mutation spends alone.
# partner: other-mutation-id  # optional; declares a conditional dependency.
                              # Revoked as orphaned if the partner leaves the
                              # committed set (discovered promotions set it too)
```

**scope DSL.** Deterministic, no model inside.

```
all
tags.type == "refusal"
and(tags.domain == "acme", or(tags.kind == "lookup", num_docs >= 4))
not(tags.needs_refusal == true)
tags.difficulty in ["easy", "medium"]
```

## What's inside (v0.5)

- **G0.** Schema/scope validation, kind rules (stateful kinds must be
  global), held-out ID leakage. Rejects before a single episode runs.
- **Decks.** Task ids must be unique within each deck, and dev, probe,
  audit and reserve must not share ids. The gate raises `ValueError`
  otherwise. A repeated id would count one lucky draw many times, and a
  shared id would let the probe re-test the dev gain. Generated items
  whose id is already known are dropped.
- **G1.** Paired measurement on the declared region only. Binary scores
  decide by exact McNemar (the sign test on discordant pairs), which
  holds its nominal size at any panel size. Continuous scores use the
  paired z-test, falling back to the sign test below 8 discordant pairs.
  Every verdict carries a 95% CI that agrees with the deciding test.
  The pre-COMMIT **transfer probe** on fresh items rejects on significant
  harm, or when the probe keeps significantly less than half of the dev
  gain (`no_transfer`). The heterogeneity scan narrows a commit to a
  subregion (`SCOPE-CORRECTED COMMIT`) only when inside beats outside
  significantly, Bonferroni-corrected over the candidates.
  `ABSTAIN` reports how much more evidence is needed.
- **G2, direction-blind synthesis.** When the dev look can't resolve, the
  gate draws fresh items inside the discordant region. It sees only the
  scope predicate, never the mutation payload or which direction it
  moved. Each round's second half joins a cumulative test pool measured
  at its own alpha; a decisive round returns COMMIT/REJECT, three
  unresolved rounds return `ABSTAIN_WITH_RECORD`. The dev deck absorbs
  the fresh items (it is the only deck allowed to grow).
- **Family look budget.** One wealth pool per mutation *family*
  (`--wealth`, `--looks`); mutations without a declared `family:` each
  get their own pool, so cross-mutation control exists only where you
  declare it. The dev measurement is look 1, synthesis rounds are the
  rest, each priced at `wealth/looks`. Decided looks refund their share;
  unresolved looks forfeit it, so a family that churns inconclusively
  drains its own budget without taxing the gate. Pool state persists in
  the ledger, so a drained family stays drained across processes. This is
  not alpha-investing or SAFFRON. Its guarantee is the bound in
  "Assumptions and limits".
- **G3, lifecycle.** After every commit, the pairwise interaction probe
  runs on the fresh deck with four arms per item (base, partner, solo,
  joint) at one draw slot, with the partner's own marginal subtracted.
  Joint gains become interaction detections; pairs whose joint falls
  below the additive expectation the score ceiling permits (real
  cancellation, not saturation; `conflict_min` sets the shortfall that
  convicts, default 0.15) get both members revoked. Abstained artifacts sit on a **deferred queue**, and
  each new commit re-measures them conditionally on committed partners
  (≥min_n overlap), where a real conditional gain promotes them at no
  budget cost. A mutation can declare `partner: <id>`; if the partner
  leaves the committed set, the orphan check revokes the dependent.
  `gate.advance_epoch(dev, probe)` is the drift boundary. Decks swap,
  every commit re-audits on the new probe reserve, measured harm revokes,
  and starved regions keep but record.
- **Lifecycle (re-audit).** `gate.audit()` / `gateadapt audit`
  re-measures each committed mutation's marginal effect and revokes only
  significant harm (a mean loss of at least 0.01 with p < alpha). In
  simulation a no-op change is revoked about 1% of the time, down from
  31% under the old rule.
- **Episode cache.** `run` results keyed by (run-name, task, config); the
  paired slot design makes repeat measurements free and deterministic.
- **ledger.jsonl.** Append-only verdict/revoke log; `committed_from_ledger`
  rebuilds the live committed set and `lifecycle_from_ledger` the rest of
  the lifecycle (deferred queue, promoted partners, interactions,
  revocations, rejections), so `gate.artifact_state` and
  `gateadapt status` answer across processes.
- **Live substrate.** `gateadapt.live.live_run` adapts any chat client to
  the `run(task, config)` contract. `gateadapt.llm.ChatClient` is a stdlib
  OpenAI-compatible client with token-bucket rate limiting (hard-capped
  below the provider's 40 rpm tier), transient-retry with per-status
  semantics, and per-call config overrides, so a mutation
  `patch: {model: X}` measures a model swap through the same gate.
  Scorers are `exact_match`, `contains`, `predicate`, and `llm_judge`;
  `llm_generate` mints synthesis items under the same direction-blind
  contract.
- **`gateadapt power`.** In-scope task-count estimate for a given effect.
- **`gateadapt replay`.** Zero-compute AutoHarness history replay on
  bundled panels.

## Exit codes (`admit`, `sequence`)

`0` for commit / scope-corrected, `1` for reject, `3` for abstain (`0`
with `--abstain-ok`). `sequence` reports where each artifact ended, so a
promoted abstain counts as passed and a revoked commit as failed.

## Honesty notes

- The scope is executable and decides the measurement region, so a
  mutation cannot claim the world and prove a corner.
- Probe items are fresh, and the gate refuses decks that share ids. A
  change that memorizes dev-task IDs scores nothing on the probe, and the
  transfer test rejects it even though its dev numbers look great.
- Synthesis is direction-blind. The item generator sees the discordant
  region's predicate, never the payload or whether the diff ran positive,
  so G2 cannot be gamed by telling the forge what to mint.
- The ledger keeps abstains and rejections alongside commits. The audit
  trail is the claim.
- Live calls are metered by a token bucket inside the client, not by
  hoped-for timing. `max_rpm` is a hard ceiling, and episodes are
  cache-keyed so a measurement never silently re-bills the provider.
- CLI `admit`/`sequence` rebuild the committed set from the ledger before
  measuring, so later mutations are evaluated stacked on the committed
  harness, matching the semantics the ledger claims.

## Assumptions and limits

- **`run` must return scores in [0, 1].** `RunSurface.draw` raises
  `ValueError` otherwise. The interaction shortfall caps the additive
  expectation at 1.0, and the harm floor (`0.01`) is absolute, so
  a scorer returning raw counts would produce silent nonsense.
- **`conflict_min = 0.15` is tuned on fixtures.** It sits between
  saturation residual (~0.10 in the demo worlds) and real cancellation
  (~0.22). It is a `Gate` constructor parameter; retune it for your
  scorer's noise.
- **`gateadapt power` is conservative.** It uses independent-arm
  variance, while the gate's paired design usually needs fewer items than
  the estimate prints.
- **The pool's error bound.** Each look is priced at α = wealth/looks and
  refunded on decision, so spent wealth equals α times the number of
  unresolved looks, capped at `looks`. A null look decides with
  probability at most α. If D is a family's count of false decisions,
  E[D] ≤ α(looks + E[D]), which gives E[D] ≤ looks·α/(1−α). With the
  defaults (wealth 0.10, looks 4, α 0.025) that is ≈ 0.103, so the
  per-family error rate stays under ~0.10 by Markov's inequality. The
  bound needs three things: each look's p-value valid conditional on the
  past, the pool actually shared (`family:` declared — unfamilied
  mutations spend alone and bound only themselves), and mutations not
  proposed adaptively from dev outcomes. A self-modifying loop does
  exactly that last part; the frozen probe and audit decks are the
  defense.
- **One writer per cache/ledger.** Episode and ledger writes are atomic,
  so a kill mid-write never corrupts them, but two processes racing on
  the same paths will interleave. Point each concurrent job at its own
  files, or serialize on the commit step.
- **`--draws N` is a slot pool, not draws per item.** Each item's pair is
  measured at one of N slots (the four-arm lifecycle probe likewise), so
  draws decorrelate episode noise across items rather than averaging it
  within one.
