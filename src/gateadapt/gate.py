"""Gate: measurement-gated admission of harness mutations.

    gate = Gate(run=my_agent, dev=load_tasks("dev.jsonl"),
                probe=load_tasks("probe.jsonl"), audit=load_tasks("audit.jsonl"))
    v = gate.admit(config, Mutation(id="prune", kind="retrieval_rule",
                                    patch={"retrieval.min_score": 0.35},
                                    scope='tags.type == "refusal"'))
    v.verdict  # COMMIT | REJECT | ABSTAIN_WITH_RECORD | SCOPE_CORRECT

Pipeline (G0 + G1):
  G0  schema + scope validity + held-out ID leakage
  G1  paired base-vs-mutated runs on the DECLARED region only; z-test on the
      paired diff; COMMIT/REJECT on significance; a pre-COMMIT transfer
      probe on the fresh probe deck; a heterogeneity/scope-correction scan
      that can narrow a commit to the subregion that actually carries the
      effect; and honest ABSTAIN_WITH_RECORD (with a task-count estimate)
      when the deck is underpowered.
"""

from __future__ import annotations

import random
from typing import Any, Callable

from .cache import EpisodeCache
from .decks import Deck, DeckRole
from .ledger import append_event
from .models import (
    ArtifactKind,
    Decision,
    FeatureSpec,
    Mutation,
    STATELESS_KINDS,
    Task,
    Verdict,
)
from .multiplicity import SaffronPool
from .mutation import apply_mutation, effective_config
from .scope import evaluate, infer_spec, to_jsonable, validate
from .stats import (
    ConditionalResult,
    PairedResult,
    eval_conditional,
    eval_pair,
    fisher_p,
    sign_test_p,
    tasks_needed,
)
from .surface import RunSurface

_EPS = 1e-6
_REVOKE_EPS = 0.05
_SIGN_TAIL = 0.20
_CONCENTRATION_FLOOR = 0.10
_MAX_DEFERRED_RETRIES = 4


def _looks_leaked(value: Any, ids: set[str]) -> bool:
    """Recursive substring hunt: does this payload mention any dev item id?"""
    if isinstance(value, str):
        return any(i in value for i in ids)
    if isinstance(value, dict):
        return any(_looks_leaked(v, ids) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_looks_leaked(v, ids) for v in value)
    return False


def g0_check(mutation: Mutation, spec: FeatureSpec, dev_ids: set[str]) -> list[str]:
    """Structural gate: schema, scope validity, leakage, kind rules."""
    violations: list[str] = []
    try:
        kind = mutation.kind_enum
    except ValueError:
        kind = ArtifactKind.PROMPT_PATCH
        violations.append(f"unknown artifact kind {mutation.kind!r}")
    if mutation.scope_fn is None:
        violations.extend(validate(mutation.scope_ast, spec))
    elif kind not in STATELESS_KINDS:
        violations.append(
            "stateful kinds must declare scope 'all'; a callable scope is not checkable"
        )
    if kind not in STATELESS_KINDS and mutation.scope_ast is not None and mutation.scope_ast != ("all",):
        violations.append(f"{kind.value} must declare scope 'all'")
    if _looks_leaked(mutation.payload, dev_ids):
        violations.append("payload leaks held-out item ids")
    if _looks_leaked(mutation.patch or {}, dev_ids):
        violations.append("patch leaks held-out item ids")
    if not mutation.patch and mutation.apply is None:
        violations.append("mutation has no patch or apply — nothing to admit")
    return violations


def _harm(measure: PairedResult) -> bool:
    """Harm rule shared by the transfer probe and re-audit."""
    return measure.diff <= -_REVOKE_EPS or (
        measure.losses > measure.wins
        and sign_test_p(measure.wins, measure.losses) < _SIGN_TAIL
    )


class Gate:
    def __init__(
        self,
        run: Callable[[Task, dict], float],
        dev: list[Task] | Deck,
        probe: list[Task] | Deck | None = None,
        audit: list[Task] | Deck | None = None,
        reserve: list[Task] | None = None,
        generate: Callable[[Callable[[Task], bool], int, random.Random], list[Task]] | None = None,
        draws: int = 3,
        alpha: float = 0.05,
        wealth: float | None = None,
        looks: int = 4,
        items_per_round: int = 24,
        refund_decided: bool = True,
        pairwise_min_gain: float = 0.05,
        min_n: int = 8,
        seed: int = 0,
        cache_path: str | None = None,
        ledger_path: str | None = None,
        name: str | None = None,
        spec: FeatureSpec | None = None,
    ):
        self.dev = dev if isinstance(dev, Deck) else Deck(DeckRole.DEV, list(dev))
        self.probe = (
            probe if isinstance(probe, Deck) or probe is None else Deck(DeckRole.PROBE, list(probe))
        )
        self.audit_deck = (
            audit if isinstance(audit, Deck) or audit is None else Deck(DeckRole.AUDIT, list(audit))
        )
        self._reserve = list(reserve) if reserve is not None else None
        self._generate = generate
        all_items = list(self.dev.items)
        for d in (self.probe, self.audit_deck):
            if d:
                all_items += d.items
        if self._reserve:
            all_items += self._reserve
        self.spec = spec or infer_spec(all_items)
        self.alpha = alpha
        # the family budget: each of `looks` looks tests at wealth/looks —
        # the dev measurement is look 1, synthesis rounds are the rest
        self.wealth = alpha * 2 if wealth is None else wealth
        self.looks = looks
        self.items_per_round = items_per_round
        self.refund_decided = refund_decided
        self.pairwise_min_gain = pairwise_min_gain
        self.min_n = min_n
        self.seed = seed
        self.ledger_path = ledger_path
        self.surface = RunSurface(run, draws=draws, name=name, cache=EpisodeCache(cache_path))
        self.committed: list[Mutation] = []
        self._pools: dict[str, SaffronPool] = {}
        self._n_decisions = 0
        # G3 lifecycle state: abstained artifacts wait here for a partner to
        # arrive; promoted ones record the partner they depend on
        self.deferred: list[list] = []  # [Mutation, retries]
        self.promoted_via: dict[str, str] = {}
        self.interactions: list[str] = []
        self.revoked: list[str] = []
        self._epoch = 0

    def _pool(self, mutation: Mutation) -> SaffronPool:
        """One SAFFRON pool per mutation family: a family that keeps producing
        indecisive members drains its own budget, not the gate's."""
        return self._pools.setdefault(
            mutation.family_name,
            SaffronPool(
                wealth=self.wealth, looks=self.looks, refund_decided=self.refund_decided
            ),
        )

    # ---- measurement helpers -------------------------------------------------

    def _rng(self, tag: str) -> random.Random:
        return random.Random(f"{self.seed}-{self._n_decisions}-{tag}")

    def _measure(
        self,
        items: list[Task],
        cfg_a: Callable[[Task], dict],
        cfg_b: Callable[[Task], dict],
        rng: random.Random,
    ) -> PairedResult:
        return eval_pair(items, lambda it: self.surface.pair(it, cfg_a(it), cfg_b(it), rng))

    def _region_measure(
        self,
        items: list[Task],
        base_config: dict,
        committed: list[Mutation],
        candidate: Mutation | None,
        scope_fn: Callable[[Task], bool],
        rng: random.Random,
    ) -> PairedResult:
        region = [t for t in items if scope_fn(t)]

        def cfg_base(t: Task) -> dict:
            return effective_config(base_config, committed, t)

        def cfg_mut(t: Task) -> dict:
            c = effective_config(base_config, committed, t)
            if candidate is not None:
                c = apply_mutation(c, candidate, t)
            return c

        return self._measure(region, cfg_base, cfg_mut, rng)

    def _transfer_probe(
        self,
        base_config: dict,
        committed: list[Mutation],
        candidate: Mutation,
        scope_fn: Callable[[Task], bool],
        rng: random.Random,
    ) -> tuple[bool, PairedResult | None]:
        """Pre-COMMIT check on fresh probe items. Pass when there is no probe
        deck or nothing to measure; fail on measured harm."""
        if self.probe is None:
            return True, None
        m = self._region_measure(self.probe.items, base_config, committed, candidate, scope_fn, rng)
        if m.n < 4:
            return True, m
        return (not _harm(m)), m

    # ---- scope correction ----------------------------------------------------

    def _scope_candidates(self, region: list[Task], scope_ast: Any) -> list[Any]:
        """Single-feature refinements of the declared scope, built from the
        declared tag vocabulary (tags.* only — data fields are payload, not a
        scoping contract) at values actually observed in-region: eq for
        str/bool fields, ge/le for numerics."""
        from .scope import _get

        conds: list[Any] = []
        seen: set[str] = set()
        for field, ftype in self.spec.fields.items():
            if not field.startswith("tags."):
                continue
            values = {
                _get(t.features, field)
                for t in region
                if _get(t.features, field) is not None
            }
            # sorted for a deterministic candidate order across processes —
            # set iteration order shifts the rng stream otherwise
            for v in sorted(values, key=repr):
                if ftype in (int, float):
                    for op in ("ge", "le"):
                        key = f"{op}:{field}:{v}"
                        if key not in seen:
                            seen.add(key)
                            conds.append(("and", scope_ast, (op, field, v)))
                elif isinstance(v, (str, bool)):
                    key = f"eq:{field}:{v}"
                    if key not in seen:
                        seen.add(key)
                        conds.append(("and", scope_ast, ("eq", field, v)))
        return conds

    def _scope_scan(
        self,
        items: list[Task],
        base_config: dict,
        committed: list[Mutation],
        candidate: Mutation,
        rng: random.Random,
    ) -> dict[str, Any]:
        """Measure effect inside vs outside each refinement; keep refinements
        that concentrate the effect (inside positive, outside ~flat)."""
        region = [t for t in items if candidate.fires(t)]
        if len(region) < 4:
            return {"cands": [], "n_tested": 0}
        base_ast = candidate.scope_ast if candidate.scope_ast is not None else ("all",)
        out = []
        n_tested = 0
        for cond in self._scope_candidates(region, base_ast):
            inside = [t for t in region if evaluate(cond, t.features)]
            outside = [t for t in region if not evaluate(cond, t.features)]
            if len(inside) < 4 or len(outside) < 4:
                continue  # a split needs evidence on both sides
            n_tested += 1
            m_in = self._region_measure(
                items, base_config, committed, candidate,
                lambda t, c=cond: evaluate(c, t.features), rng,
            )
            m_out = self._region_measure(
                items, base_config, committed, candidate,
                lambda t, c=cond: candidate.fires(t) and not evaluate(c, t.features), rng,
            )
            # concentrated = inside positive AND outside gain is immaterial
            # (< CONCENTRATION_FLOOR). A sub-floor outside benefit doesn't
            # justify forfeiting the declared scope's claim; significance
            # of the inside effect is still enforced by Bonferroni below.
            concentrated = m_in.diff > _EPS and m_out.diff <= _CONCENTRATION_FLOOR
            if concentrated:
                out.append({"cond": cond, "in": m_in, "out": m_out})
        out.sort(key=lambda c: c["in"].p)
        return {"cands": out, "n_tested": n_tested}

    def _accept_correction(
        self,
        scan: dict[str, Any],
        base_config: dict,
        committed: list[Mutation],
        candidate: Mutation,
        alpha: float,
        rng: random.Random,
    ) -> dict[str, Any] | None:
        """Accept the best scan candidate that survives Bonferroni on dev
        plus a probe-side confirm (Fisher combine + no measured harm)."""
        cands = scan["cands"]
        n_tested = max(scan["n_tested"], 1)
        for cand in cands[:3]:
            m_in = cand["in"]
            if m_in.p >= alpha / n_tested:
                continue
            narrowed = Mutation(
                id=candidate.id,
                kind=candidate.kind,
                patch=candidate.patch,
                scope=cand["cond"],
                family=candidate.family,
                payload=candidate.payload,
                apply=candidate.apply,
            )
            probe_ok = True
            probe_p = None
            if self.probe is not None:
                pm = self._region_measure(
                    self.probe.items, base_config, committed, narrowed, narrowed.fires, rng
                )
                probe_p = pm.p
                ok, _ = self._transfer_probe(
                    base_config, committed, narrowed, narrowed.fires, rng
                )
                probe_ok = ok and (pm.n < 4 or fisher_p(m_in.p, pm.p) < alpha / n_tested)
            if probe_ok:
                return {
                    "narrowed": narrowed,
                    "m_in": m_in,
                    "m_out": cand["out"],
                    "probe_p": probe_p,
                    "n_tested": n_tested,
                }
        return None

    def _scope_correct(
        self,
        base_config: dict,
        committed: list[Mutation],
        candidate: Mutation,
        alpha: float,
        rng: random.Random,
    ) -> dict[str, Any] | None:
        """Scan refinements on dev; accept the best that survives Bonferroni.
        Callable scopes can't be refined (nothing to introspect)."""
        if candidate.scope_fn is not None:
            return None
        scan = self._scope_scan(self.dev.items, base_config, committed, candidate, rng)
        return self._accept_correction(scan, base_config, committed, candidate, alpha, rng)

    # ---- G2: direction-blind synthesis ----------------------------------------

    def _draw_fresh(
        self, region_fn: Callable[[Task], bool], n: int, rng: random.Random
    ) -> list[Task]:
        """Fresh in-region items for a synthesis round: from the user's
        generator callable, or the consumable reserve deck (drawn without
        replacement)."""
        if self._generate is not None:
            # re-filter through the region predicate: the generator sees the
            # discordant region, but only in-region items may join the pool
            return [t for t in self._generate(region_fn, n, rng) if region_fn(t)]
        if self._reserve is None:
            return []
        rng.shuffle(self._reserve)
        taken = [t for t in self._reserve if region_fn(t)][:n]
        taken_ids = {t.id for t in taken}
        self._reserve = [t for t in self._reserve if t.id not in taken_ids]
        return taken

    def _synthesize(
        self,
        base_config: dict,
        committed: list[Mutation],
        resolved: Mutation,
        pool: SaffronPool,
        rng: random.Random,
    ) -> tuple[Verdict, list[dict[str, Any]]]:
        """Direction-blind synthesis: only the discordant region is described —
        never the mutation's payload or sign. Fresh items join the dev deck
        (the only deck that may grow); each round's second half joins a
        cumulative test pool measured at the look's own alpha. A decided look
        refunds its quarter; an unresolved look forfeits it."""
        rounds: list[dict[str, Any]] = []
        test_pool: list[Task] = []
        for k in range(1, self.looks):  # look 1 was the dev measurement
            alpha_k = pool.allocate()
            if alpha_k <= 0:
                rounds.append({"k": k, "reason": "looks_exhausted"})
                break
            pool.spend(alpha_k)
            fresh = self._draw_fresh(resolved.fires, self.items_per_round, rng)
            if fresh:
                self.dev.add(fresh)
                test_pool.extend(fresh[len(fresh) // 2 :])
            m = self._region_measure(
                test_pool, base_config, committed, resolved, resolved.fires, rng
            )
            decided = m.n >= self.min_n and m.p < alpha_k
            v = (Verdict.COMMIT if m.diff > 0 else Verdict.REJECT) if decided else None
            rounds.append(
                {
                    "k": k,
                    "items_drawn": len(fresh),
                    "test_n": m.n,
                    "diff": m.diff,
                    "p": m.p,
                    "alpha": alpha_k,
                    "verdict": v.value if v else "inconclusive",
                }
            )
            if v is not None:
                pool.refund(alpha_k)
                return v, rounds
        return Verdict.ABSTAIN_WITH_RECORD, rounds

    # ---- G3: lifecycle probe, conditional promotion, drift --------------------

    def _conditional(
        self,
        items: list[Task],
        base_config: dict,
        x: Mutation,
        y: Mutation,
        rng: random.Random,
    ) -> ConditionalResult:
        """4-arm paired interaction estimate: x's marginal with partner y vs
        without, minus y's own marginal. One slot drives all four configs —
        base (committed minus the pair), y alone, x alone, joint."""
        others = [c for c in self.committed if c is not x and c.id != y.id]

        def quad(t: Task) -> tuple[float, float, float, float]:
            idx = rng.randrange(self.surface.draws)
            cfg_base = effective_config(base_config, others, t)
            cfg_y = effective_config(base_config, others + [y], t)
            cfg_x = apply_mutation(cfg_base, x, t)
            cfg_xy = apply_mutation(cfg_y, x, t)
            return (
                self.surface.draw(t, cfg_base, idx),
                self.surface.draw(t, cfg_y, idx),
                self.surface.draw(t, cfg_x, idx),
                self.surface.draw(t, cfg_xy, idx),
            )

        return eval_conditional(items, quad)

    def _revoke(
        self, mutation: Mutation, reason: str, detail: dict | None = None
    ) -> dict[str, Any]:
        """Remove a committed artifact: bookkeeping + ledger event. Revocation
        is an event, not a deletion — the artifact stays on the record."""
        self.committed = [c for c in self.committed if c is not mutation]
        self.promoted_via.pop(mutation.id, None)
        if mutation.id not in self.revoked:
            self.revoked.append(mutation.id)
        ev = {"event": "revoke", "artifact_id": mutation.id, "reason": reason}
        if detail:
            ev["detail"] = detail
        if self.ledger_path:
            append_event(
                self.ledger_path,
                {"event": "revoke", "artifact_id": mutation.id, "detail": {"reason": reason, **(detail or {})}},
            )
        return ev

    def _overlap(self, x: Mutation, y: Mutation) -> list[Task]:
        """Probe items where both scopes fire — the interaction region."""
        if self.probe is None:
            return []
        return [t for t in self.probe.items if x.fires(t) and y.fires(t)]

    def _lifecycle_probe(
        self, base_config: dict, rng: random.Random
    ) -> list[dict[str, Any]]:
        """After every commit: orphan check (a conditional artifact whose
        partner left the set is revoked), then the pairwise interaction probe
        — joint gains become interaction detections, joint harms revoke the
        artifact whose effect inverted. Runs on the fresh probe deck."""
        events: list[dict[str, Any]] = []
        committed_ids = {m.id for m in self.committed}
        for m in list(self.committed):
            dep = m.partner or self.promoted_via.get(m.id)
            if dep and dep not in committed_ids:
                events.append(
                    self._revoke(m, "partner_absent", {"partner": dep})
                )
        live = list(self.committed)
        for i, x in enumerate(live):
            for y in live[i + 1 :]:
                live_ids = {m.id for m in self.committed}
                if x.id not in live_ids or y.id not in live_ids:
                    continue
                region = self._overlap(x, y)
                if len(region) < self.min_n:
                    continue
                for a, b in ((x, y), (y, x)):
                    if a.id not in {m.id for m in self.committed}:
                        continue  # already revoked by the other direction
                    res = self._conditional(region, base_config, a, b, rng)
                    # generic evaluators return noisy zero for a non-
                    # interaction — require a discordant-gain skew so noise
                    # alone can't fabricate a detection or a conflict
                    decisive = sign_test_p(res.pos, res.neg) < _SIGN_TAIL
                    if res.gain >= self.pairwise_min_gain and decisive:
                        pair = f"{a.id}+{b.id}"
                        if pair not in self.interactions:
                            self.interactions.append(pair)
                        ev = {"event": "interaction", "pair": pair, "gain": res.gain}
                        events.append(ev)
                        if self.ledger_path:
                            append_event(self.ledger_path, {**ev})
                    elif res.gain <= -self.pairwise_min_gain and decisive:
                        events.append(
                            self._revoke(
                                a,
                                "interaction_conflict",
                                {"partner": b.id, "gain": res.gain},
                            )
                        )
        return events

    def _retry_deferred(
        self, base_config: dict, rng: random.Random
    ) -> list[dict[str, Any]]:
        """Each new commit re-opens the deferred queue: an artifact that
        abstained inert now gets measured *conditional on* each committed
        partner whose region overlaps. A conditional gain promotes it — the
        lifecycle instrument, costing no SAFFRON look. Retries only count
        when an overlap region was actually probed."""
        events: list[dict[str, Any]] = []
        still: list[list] = []
        for m, retries in self.deferred:
            if retries >= _MAX_DEFERRED_RETRIES:
                still.append([m, retries])
                continue
            promoted = probed = False
            for b in list(self.committed):
                region = self._overlap(m, b)
                if len(region) < self.min_n:
                    continue
                probed = True
                res = self._conditional(region, base_config, m, b, rng)
                if (
                    res.gain >= self.pairwise_min_gain
                    and res.joint_diff > 0
                    and sign_test_p(res.pos, res.neg) < _SIGN_TAIL
                ):
                    # promotion is conditional on b: the artifact's commit is
                    # valid only while b stays committed
                    m.partner = m.partner or b.id
                    self.promoted_via[m.id] = b.id
                    self.committed.append(m)
                    pair = f"{m.id}+{b.id}"
                    if pair not in self.interactions:
                        self.interactions.append(pair)
                    ev = {
                        "event": "promote",
                        "artifact_id": m.id,
                        "partner": b.id,
                        "gain": res.gain,
                    }
                    events.append(ev)
                    detail = {
                        "via_deferred": True,
                        "partner": b.id,
                        "gain": res.gain,
                        "joint_diff": res.joint_diff,
                        "region_n": res.n,
                    }
                    if self.ledger_path:
                        append_event(
                            self.ledger_path,
                            {
                                "event": "verdict",
                                "artifact": m.to_dict(),
                                "verdict": Verdict.COMMIT.value,
                                "detail": detail,
                                "declared_scope": m.scope_text,
                                "episodes": self.surface.episodes,
                            },
                        )
                    events += self._lifecycle_probe(base_config, rng)
                    promoted = True
                    break
            if not promoted:
                still.append([m, retries + (1 if probed else 0)])
        self.deferred = still
        return events

    def _after_commit(
        self, base_config: dict, rng: random.Random
    ) -> list[dict[str, Any]]:
        return self._lifecycle_probe(base_config, rng) + self._retry_deferred(
            base_config, rng
        )

    def shift(self, dev: list[Task] | None = None, probe: list[Task] | None = None) -> None:
        """Epoch boundary: swap in today's decks. Drift is a swap, not growth —
        the probe reserve always models the population the gate now serves.
        The cache namespaces by epoch so stale episodes can't be re-served
        for same-id items under a shifted world."""
        self._epoch += 1
        self.surface.name = f"{self.surface.name}@{self._epoch}"
        if dev is not None:
            self.dev = Deck(DeckRole.DEV, list(dev))
        if probe is not None:
            self.probe = Deck(DeckRole.PROBE, list(probe))

    def advance_epoch(
        self,
        base_config: dict,
        dev: list[Task] | None = None,
        probe: list[Task] | None = None,
    ) -> list[Decision]:
        """Drift epoch: shift the decks, then re-audit every committed
        mutation on the new probe reserve — measured harm revokes, a starved
        region keeps but records."""
        self.shift(dev=dev, probe=probe)
        tasks = self.probe.items if self.probe is not None else None
        return self.audit(base_config, tasks=tasks)

    # ---- the admission pipeline ----------------------------------------------

    def admit(self, base_config: dict, mutation: Mutation) -> Decision:
        rng = self._rng(mutation.id)
        self._n_decisions += 1
        pool = self._pool(mutation)
        dev_ids = {t.id for t in self.dev.items}

        violations = g0_check(mutation, self.spec, dev_ids)
        if violations:
            return self._record(mutation, Verdict.REJECT, {"g0_violations": violations}, rng)

        # look 1: the dev measurement, at the look's own alpha (wealth/looks)
        alpha1 = pool.allocate()
        if alpha1 <= 0:
            return self._record(
                mutation,
                Verdict.ABSTAIN_WITH_RECORD,
                {"reason": "family_budget_exhausted"},
                rng,
            )
        pool.spend(alpha1)
        looks_trace: list[dict[str, Any]] = [{"look": 1, "alpha": alpha1, "where": "dev"}]

        m = self._region_measure(
            self.dev.items, base_config, self.committed, mutation, mutation.fires, rng
        )
        base_detail = {
            "region_n": m.n,
            "dev_n": len(self.dev.items),
            "diff": m.diff,
            "diff_se": m.diff_se,
            "p": m.p,
            "wins": m.wins,
            "losses": m.losses,
            "base_mean": m.base_mean,
            "alpha": alpha1,
            "looks": looks_trace,
        }
        significant = m.n >= self.min_n and m.p < alpha1

        if significant and m.diff > 0:
            # heterogeneity guard: if the effect concentrates in a subregion,
            # the honest verdict is a commit on the narrowed scope.
            corrected = self._scope_correct(base_config, self.committed, mutation, alpha1, rng)
            if corrected is not None:
                pool.refund(alpha1)
                return self._scope_corrected(mutation, corrected, base_detail, rng, base_config)
            ok, probe_m = self._transfer_probe(
                base_config, self.committed, mutation, mutation.fires, rng
            )
            if not ok:
                pool.refund(alpha1)
                base_detail["caught_by"] = "transfer_probe"
                base_detail["probe"] = {
                    "n": probe_m.n,
                    "diff": probe_m.diff,
                    "wins": probe_m.wins,
                    "losses": probe_m.losses,
                }
                return self._record(mutation, Verdict.REJECT, base_detail, rng)
            pool.refund(alpha1)
            base_detail["transfer_probe"] = (
                "skipped (no probe deck)" if probe_m is None else "pass"
            )
            return self._commit(mutation, base_detail, rng, base_config)

        if significant and m.diff < 0:
            corrected = self._scope_correct(base_config, self.committed, mutation, alpha1, rng)
            if corrected is not None:
                pool.refund(alpha1)
                return self._scope_corrected(mutation, corrected, base_detail, rng, base_config)
            pool.refund(alpha1)
            return self._record(mutation, Verdict.REJECT, base_detail, rng)

        # inconclusive — scope scan first (rescue a heterogeneous subregion),
        # then direction-blind synthesis if an item source exists.
        scan = self._scope_scan(self.dev.items, base_config, self.committed, mutation, rng)
        corrected = self._accept_correction(scan, base_config, self.committed, mutation, alpha1, rng)
        if corrected is not None:
            pool.refund(alpha1)
            return self._scope_corrected(mutation, corrected, base_detail, rng, base_config)

        resolved = mutation
        if scan["cands"]:
            # a heterogeneous subregion was seen even if it didn't clear the
            # scan — synthesis steers into it, direction-blind
            narrowed = Mutation(
                id=mutation.id,
                kind=mutation.kind,
                patch=mutation.patch,
                scope=scan["cands"][0]["cond"],
                family=mutation.family,
                payload=mutation.payload,
                apply=mutation.apply,
                partner=mutation.partner,
            )
            resolved = narrowed

        if self._generate is not None or self._reserve is not None:
            v2, rounds = self._synthesize(base_config, self.committed, resolved, pool, rng)
            base_detail["synthesis_rounds"] = rounds
            if v2 == Verdict.COMMIT:
                ok, probe_m = self._transfer_probe(
                    base_config, self.committed, resolved, resolved.fires, rng
                )
                if ok:
                    if resolved is not mutation:
                        best = scan["cands"][0]
                        return self._scope_corrected(
                            mutation,
                            {"narrowed": resolved, "m_in": best["in"], "m_out": best["out"]},
                            base_detail,
                            rng,
                            base_config,
                        )
                    return self._commit(mutation, base_detail, rng, base_config)
                base_detail["caught_by"] = "transfer_probe"
                return self._record(mutation, Verdict.REJECT, base_detail, rng)
            if v2 == Verdict.REJECT:
                return self._record(mutation, Verdict.REJECT, base_detail, rng)

        need = tasks_needed(
            effect=max(abs(m.diff), 0.05), base=max(m.base_mean, 0.02), alpha=self.alpha
        )
        base_detail["needed"] = {
            "have": m.n,
            "need": max(need - m.n, 0),
            "at_effect": max(abs(m.diff), 0.05),
        }
        base_detail["reason"] = (
            "inconclusive" if m.n >= self.min_n else "region_too_small"
        )
        # a family-exhausted abstain is terminal (the family can't afford
        # another look); any other abstain waits on the deferred queue —
        # a later commit may reveal it as a conditional interaction
        if base_detail["reason"] != "family_budget_exhausted":
            self.deferred.append([mutation, 0])
        return self._record(mutation, Verdict.ABSTAIN_WITH_RECORD, base_detail, rng)

    # ---- verdict writers ------------------------------------------------------

    def _commit(self, mutation: Mutation, detail: dict, rng: random.Random, base_config: dict) -> Decision:
        self.committed.append(mutation)
        decision = self._record(mutation, Verdict.COMMIT, detail, rng)
        events = self._after_commit(base_config, rng)
        if events:
            decision.detail["lifecycle"] = events
        return decision

    def _scope_corrected(self, mutation: Mutation, corrected: dict, detail: dict, rng, base_config: dict) -> Decision:
        narrowed = corrected["narrowed"]
        self.committed.append(narrowed)
        detail["narrowed_scope"] = to_jsonable(narrowed.scope_ast)
        detail["heterogeneity"] = {
            "diff_in": corrected["m_in"].diff,
            "n_in": corrected["m_in"].n,
            "diff_out": corrected["m_out"].diff,
            "n_out": corrected["m_out"].n,
        }
        decision = self._record(narrowed, Verdict.SCOPE_CORRECT, detail, rng, original=mutation)
        events = self._after_commit(base_config, rng)
        if events:
            decision.detail["lifecycle"] = events
        return decision

    def _record(
        self,
        mutation: Mutation,
        verdict: Verdict,
        detail: dict,
        rng: random.Random,
        original: Mutation | None = None,
    ) -> Decision:
        decision = Decision(artifact_id=mutation.id, verdict=verdict, detail=detail)
        self.surface.flush()
        if self.ledger_path:
            append_event(
                self.ledger_path,
                {
                    "event": "verdict",
                    "artifact": mutation.to_dict(),
                    "verdict": verdict.value,
                    "detail": detail,
                    "declared_scope": (original or mutation).scope_text,
                    "episodes": self.surface.episodes,
                },
            )
        return decision

    # ---- lifecycle: re-audit committed mutations ------------------------------

    def audit(self, base_config: dict, tasks: list[Task] | None = None) -> list[Decision]:
        """Re-measure every committed mutation's marginal effect (committed set
        vs committed-minus-it) on its region. Measured harm revokes; a starved
        region keeps but records. Each revocation is a ledger event."""
        pool = tasks or self.dev.items
        decisions: list[Decision] = []
        for m in list(self.committed):
            rng = self._rng(f"reaudit-{m.id}")
            others = [c for c in self.committed if c is not m]
            res = self._region_measure(pool, base_config, others, m, m.fires, rng)
            detail = {
                "n": res.n,
                "diff": res.diff,
                "wins": res.wins,
                "losses": res.losses,
                "revoke_rule": f"diff <= -{_REVOKE_EPS} or sign-tail < {_SIGN_TAIL}",
            }
            if res.n < self.min_n:
                detail["status"] = "kept_scope_starved"
            elif _harm(res):
                self._revoke(m, "drift_harm", {"diff": res.diff})
                detail["status"] = "revoked"
            else:
                detail["status"] = "kept"
            if self.ledger_path:
                append_event(
                    self.ledger_path,
                    {
                        "event": "reaudit",
                        "artifact_id": m.id,
                        "verdict": detail["status"],
                        "detail": detail,
                    },
                )
            decisions.append(
                Decision(
                    artifact_id=m.id,
                    verdict=Verdict.REJECT if detail["status"] == "revoked" else Verdict.COMMIT,
                    detail=detail,
                )
            )
        if any(d.detail.get("status") == "revoked" for d in decisions):
            # a revoked partner can orphan a conditional artifact — rerun
            # the lifecycle probe so dependents do not linger
            self._lifecycle_probe(base_config, self._rng("post-audit"))
        self.surface.flush()
        return decisions

    def audit_report(self, base_config: dict) -> dict[str, Any]:
        """The honest read: committed set vs unmutated base, on the frozen
        audit deck. Never touches admission."""
        if self.audit_deck is None or not self.committed:
            return {"n": 0, "diff": 0.0}
        rng = self._rng("audit-report")
        res = self._measure(
            self.audit_deck.items,
            lambda t: dict(base_config),
            lambda t: effective_config(base_config, self.committed, t),
            rng,
        )
        return {
            "n": res.n,
            "diff": res.diff,
            "diff_se": res.diff_se,
            "wins": res.wins,
            "losses": res.losses,
            "p": res.p,
        }
