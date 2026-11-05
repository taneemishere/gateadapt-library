# GateAdapt

**CI for agent changes.** You supply:

1. `run(task, config) -> score` — your agent's eval on one task
2. a tasks file (JSONL) with tags, split into **dev**, **probe**, and **audit** decks
3. a **mutation** — `id`, `kind`, `patch`, and a deterministic **scope**

Every change declares *where* it claims to help (its scope). It is tested
*only there*, on paired runs (same task, same draw, before vs after). The
gate answers:

- **COMMIT** — the change measurably helps in its declared scope and the
  fresh-item probe shows no harm
- **REJECT** — it harms, leaks held-out data, or is structurally invalid
- **ABSTAIN** — *not enough evidence yet*; the verdict tells you how many
  more in-scope tasks you need
- **SCOPE-CORRECTED COMMIT** — it helps, but only in a subregion; the gate
  admits it on the narrower scope instead of crediting the whole claim

Committed changes are re-measured later and can be **revoked**. Every
verdict and revocation is appended to `ledger.jsonl` — the decision log is
part of the artifact.

Abstaining is a first-class outcome: the gate exists because whole-suite
pass-rate diffs are noisy (AutoHarness dev20: 6/20 tasks flip on an
*unchanged* harness), and certifying noise is worse than admitting nothing.

## Install

```bash
pip install -e ".[dev]"
```

## 5-minute quickstart

The repo ships a deterministic toy agent (no API key, zero compute):

```bash
gateadapt admit examples/mutations/refusal_contract.yaml \
    --config examples/config.yaml \
    --tasks examples/tasks_dev.jsonl \
    --probe examples/tasks_probe.jsonl \
    --run examples/toy_agent.py:run \
    --ledger ledger.jsonl
```

```
verdict: commit
  region: 24/48 items · paired diff +0.292 (se 0.095, p 0.0021) · wins 7 losses 0
  transfer probe: pass
```

Try the other demo mutations — they cover the full verdict space:

| mutation | verdict | why |
|---|---|---|
| `refusal_contract.yaml` | `commit` | +0.29 on refusals, probe clean |
| `always_answer.yaml` | `reject` | −0.17 paired diff, significant |
| `topk_boost.yaml` | `abstain_with_record` | +0.04 on 24 items — underpowered |
| `prune_hits.yaml` | `scope_correct` | helps only `tags.domain == "acme"` |
| `memoize_dev.yaml` | `reject` | helps dev, harms fresh → transfer probe catches it |
| `answer_lift.yaml` | `commit` (via G2) | underpowered on dev → synthesis resolves it |

`answer_lift` is the G2 story: too weak to certify at dev scale, so the gate
synthesizes fresh in-region items until the cumulative test pool resolves it:

```bash
gateadapt admit examples/mutations/answer_lift.yaml \
    --config examples/config.yaml \
    --tasks examples/tasks_dev.jsonl \
    --probe examples/tasks_probe.jsonl \
    --reserve examples/tasks_reserve.jsonl \
    --items-per-round 48 \
    --run examples/toy_agent.py:run
```

```
verdict: commit
  region: 24/48 items · paired diff +0.167 (se 0.078, p 0.0320) · wins 4 losses 0
  synthesis round 1: drew 48 fresh · test pool 24 · diff +0.083 (p 0.1482 @ α 0.025) · inconclusive
  synthesis round 2: drew 48 fresh · test pool 48 · diff +0.167 (p 0.0022 @ α 0.025) · commit
```

No reserve deck? Pass `--generate examples/toy_agent.py:generate` — a
callable `(region_fn, n, rng) -> [Task]` that mints in-region items (your
task forge goes here in a real deployment).

### The lifecycle story (G3)

Feed a *stream* of mutations through one persistent gate:

```bash
gateadapt sequence \
    examples/mutations/needs_pair_a.yaml \
    examples/mutations/needs_pair_b.yaml \
    --config examples/config.yaml --tasks examples/tasks_dev.jsonl \
    --probe examples/tasks_probe.jsonl --run examples/toy_agent.py:run
```

```
needs-pair-a:
verdict: abstain_with_record        # inert alone — deferred, not dead
needs-pair-b:
verdict: commit
  lifecycle: promoted deferred needs-pair-a (conditional gain +0.21 with needs-pair-b)
  lifecycle: interaction detected needs-pair-b+needs-pair-a (gain +0.23)
```

The abstained artifact wasn't wrong — it was an interaction arriving before
its partner. When the partner commits, the gate measures it *conditionally*
and promotes it without spending a SAFFRON look. And the dark side:

```bash
gateadapt sequence examples/mutations/conflict_a.yaml examples/mutations/conflict_b.yaml \
    --config examples/config.yaml --tasks examples/tasks_dev.jsonl \
    --probe examples/tasks_probe.jsonl --run examples/toy_agent.py:run
```

Both commit — each marginal is honestly positive. Then the pairwise probe
on fresh items finds the cannibalization (joint < either solo) and revokes
both: `lifecycle: revoked conflict-a / conflict-b (interaction_conflict)`.

### Live substrate (real LLM episodes)

The same gate drives a real model — `run(task, config)` is just a contract.
`gateadapt.live` turns any `.chat(messages)` client into one; `gateadapt.llm`
ships a stdlib OpenAI-compatible client with a hard token-bucket rate
limiter (default 36 rpm) and transient-retry. A mutation's `patch` reaches
the wire per call: `system_prompt`, `prompt_suffix`, `temperature`,
`max_tokens`, `seed`, `model`.

```bash
gateadapt admit examples/mutations/strict_answers.yaml \
    --config examples/config.yaml \
    --tasks examples/tasks_live_dev.jsonl \
    --probe examples/tasks_live_probe.jsonl \
    --run examples/live_agent.py:run \
    --draws 1 --ledger live_ledger.jsonl --cache live_episodes.json
```

```
verdict: commit
  region: 9/16 items · paired diff +0.667 (se 0.167, p 0.0001) · wins 6 losses 0
  transfer probe: pass
```

Real run, real episodes: nemotron-3-ultra-550b via NIM (key from
`NVIDIA_API_KEY` or `--keyfile`), 32 API calls, ~10 min wall-clock —
the model's verbose "2 + 2 = 4" fails `exact_match("4")`, the output
contract fixes it. The episode cache keys each call by (task, config)
fingerprint, so a rerun measures nothing twice — interrupt and resume
freely.

The full verdict space has been exercised live on that substrate:

| live mutation | verdict | evidence |
|---|---|---|
| `strict_answers` | `commit` | +0.667 on answers, probe pass |
| `refusal_contract_live` | `commit` | +1.000 on refusals (stacked on the first) |
| `live_unknown` | `reject` | −0.625 — sabotage contract kills the stack's answers |
| `temp_tune` | `abstain_with_record` | +0.125 on 8 items, p=0.32 — "need ~204 more" |
| `context_only` | `scope_correct` | declared `all`, concentrated in `tags.type == "refusal"` |
| `brief_answer` | `commit` via **G2** | dev p=0.0404 inconclusive → LLM-minted round: 22 fresh items, pool +0.545 (p=0.0005) |
| `sequence` strict→terse→refusal | **G3** lifecycle | terse abstains → deferred → conditional retry on strict's overlap ran (real 4-arm measurement), clean pair probe |

The last row is the honest G3 live read: the machinery ran the 4-arm
conditional measurement on real episodes, found no interaction (disjoint
scopes), and emitted no false event — a clean pair logs nothing.

The qa-harness dogfood (`examples/qa-harness_agent.py`) ran the research's five
curated corpus-QA artifacts as a real stream — retrieval + LLM + `run_gates`
episodes, stratified dev/probe/reserve decks, SAFFRON pools per mutation.
On nemotron-3-ultra-550b all five abstained: the corpus is 39/41 freeform
golds at a ~0.79 base, so there was no headroom — a +0.214 dev signal was
dissolved by a 0.000 test-pool draw (synthesis consumed 12 real reserve
items). On the smaller nemotron-3.5-lightning-30b the signal *moved* —
concise/assertive/topk trended −0.09…−0.13 (prompt edits and extra
retrieved docs hurt a weaker model) while the refusal contract stayed
mildly positive — and the gate again abstained rather than certify either
direction. A `top_k: 0` zero-retrieval ablation on the same substrate
produced the live `reject`: −0.714, 0-10, p<0.0001 (a gentler over-refusal
sabotage abstained honestly — it only flipped the borderline tasks).
Episode caches are namespaced by model identity
(`run.__qualname__` → `RunSurface.name` → cache key), so switching
`GATEADAPT_LLM_MODEL` never serves stale episodes.

```python
from gateadapt.llm import NvidiaNIMClient        # or any .chat() client
from gateadapt.live import live_run, exact_match, llm_judge, llm_generate

llm = NvidiaNIMClient.from_env(max_rpm=36)      # hard-capped under the 40 rpm tier
run = live_run(llm, exact_match())              # or contains(), predicate(fn), llm_judge(llm)
gate = Gate(run=run, dev=..., probe=..., generate=llm_generate(llm, describe="..."))
```

Then replay AutoHarness's shipped optimization history (bundled panels,
zero compute):

```bash
gateadapt replay --model both
gateadapt power --effect 0.10          # how many in-scope tasks a verdict needs
gateadapt power --effect 0.10 --tasks examples/tasks_dev.jsonl --scope 'tags.type == "answer"'
```

## Python API

```python
from gateadapt import Gate, Mutation, load_tasks

gate = Gate(
    run=my_agent,                                # run(task, config) -> score in [0,1]
    dev=load_tasks("tasks_dev.jsonl"),           # decides admission; grows with synthesized items
    probe=load_tasks("tasks_probe.jsonl"),       # fresh items, pre-commit transfer check
    audit=load_tasks("tasks_audit.jsonl"),       # frozen: carries reported claims only
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

# the honest read: committed set vs base, on the frozen audit deck
gate.audit_report(config)
```

Subsequent `admit` calls measure each candidate **stacked on the committed
harness** — the base arm is the current admitted state, so a second commit
must beat the first, not the unmutated origin.

## Inputs

**tasks (JSONL)** — one object per line; `id` required, `tags` is the scope
feature surface, everything else is passed through to `run` as `task.data`:

```jsonl
{"id": "t-001", "tags": {"type": "refusal", "domain": "acme"}, "question": "...", "p": 0.52}
```

**mutation (YAML/JSON)**:

```yaml
id: prune-weak-hits
kind: retrieval_rule          # prompt_patch | tool_schema_patch | retrieval_rule |
                              # budget_rule | contract_patch | routing_rule | memory_rule
patch:
  retrieval.min_score: 0.35   # dotted keys deep-merge into the effective config
scope: 'tags.type == "refusal"'
# family: rerank-motif        # optional — shares one SAFFRON pool per family
# partner: other-mutation-id  # optional — declares a conditional dependency:
                              # revoked as orphaned if the partner leaves the
                              # committed set (discovered promotions set it too)
```

**scope DSL** — deterministic, no model inside:

```
all
tags.type == "refusal"
and(tags.domain == "acme", or(tags.kind == "lookup", num_docs >= 4))
not(tags.needs_refusal == true)
tags.difficulty in ["easy", "medium"]
```

## What's inside (v0.4)

- **G0** — schema/scope validation, kind rules (stateful kinds must be
  global), held-out ID leakage. Rejects before a single episode runs.
- **G1** — paired measurement on the declared region only; z-test on the
  paired diff; pre-COMMIT **transfer probe** on fresh probe items;
  heterogeneity scan that narrows a commit to the subregion carrying the
  effect (`SCOPE-CORRECTED COMMIT`); `ABSTAIN` with a task-count estimate.
- **G2 — direction-blind synthesis.** When the dev look can't resolve, the
  gate draws fresh items inside the discordant region — it sees only the
  scope predicate, never the mutation payload or which direction it moved.
  Each round's second half joins a cumulative test pool measured at its own
  alpha; a decisive round returns COMMIT/REJECT, three unresolved rounds
  return `ABSTAIN_WITH_RECORD`. The dev deck absorbs the fresh items (it's
  the only deck allowed to grow).
- **SAFFRON alpha-investing** — one shared wealth pool per mutation *family*
  (`--wealth`, `--looks`): the dev measurement is look 1, synthesis rounds
  are the rest, each priced at `wealth/looks`. Decided looks refund their
  quarter; unresolved looks forfeit it — a family that churns inconclusively
  drains its own budget without taxing the gate.
- **G3 — lifecycle.** After every commit, the pairwise interaction probe
  runs on the fresh deck: four arms per item (base, partner, solo, joint)
  at one draw slot, partner's own marginal subtracted. Joint gains become
  interaction detections; subadditive pairs get both members revoked.
  Abstained artifacts sit on a **deferred queue** — each new commit
  re-measures them conditionally on committed partners (≥min_n overlap),
  and a real conditional gain promotes them at no SAFFRON cost. A mutation
  can declare `partner: <id>` — if the partner leaves the committed set,
  the orphan check revokes the dependent. `gate.advance_epoch(dev, probe)`
  is the drift boundary: decks swap, every commit re-audits on the new
  probe reserve, measured harm revokes, starved regions keep but record.
- **Lifecycle (re-audit)** — `gate.audit()` / `gateadapt audit` re-measures
  each committed mutation's marginal effect and revokes measured harm.
- **Episode cache** — `run` results keyed by (run-name, task, config); the
  paired slot design makes repeat measurements free and deterministic.
- **ledger.jsonl** — append-only verdict/revoke log; `committed_from_ledger`
  rebuilds the live committed set.
- **Live substrate** — `gateadapt.live.live_run` adapts any chat client to
  the `run(task, config)` contract; `gateadapt.llm.ChatClient` is a stdlib
  OpenAI-compatible client with token-bucket rate limiting (hard-capped
  below the provider's 40 rpm tier), transient-retry with per-status
  semantics, and per-call config overrides — a mutation `patch: {model: X}`
  measures a model swap through the same gate. Scorers: `exact_match`,
  `contains`, `predicate`, `llm_judge`; `llm_generate` mints synthesis items
  under the same direction-blind contract.
- **`gateadapt power`** — in-scope task-count estimate for a given effect.
- **`gateadapt replay`** — zero-compute AutoHarness history replay on
  bundled panels.

Deliberately deferred: hosted integrations (GitHub Action etc.) — the live
substrate is in; the hosted layer lands as an optional extra.

## Exit codes (`admit`)

`0` commit / scope-corrected / abstain · `1` reject. Abstain is neutral by
design — in CI it should not fail the build.

## Honesty notes

- The scope is executable and decides the measurement region — a mutation
  cannot claim the world and prove a corner.
- Probe items are fresh: a change that memorizes dev-task IDs is caught by
  the transfer probe even though its dev numbers look great.
- Synthesis is direction-blind: the item generator sees the discordant
  region's predicate, never the payload or whether the diff ran positive —
  so G2 can't be gamed by telling the forge what to mint.
- The ledger keeps abstains and rejections, not just commits — the audit
  trail is the claim.
- Live calls are metered by a token bucket inside the client, not by hoped-
  for timing: `max_rpm` is a hard ceiling, and episodes are cache-keyed so a
  measurement never silently re-bills the provider.
- CLI `admit`/`sequence` rebuild the committed set from the ledger before
  measuring — later mutations are evaluated stacked on the committed
  harness, matching the semantics the ledger claims.
