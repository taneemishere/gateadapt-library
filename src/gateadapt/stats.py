"""Paired measurement + decision statistics.

The pairing is the point: every in-scope item produces a (base, mutated)
score pair from the same task, and the diff distribution — not the pass
rate — is what the z-test consumes. Binary scores additionally give
discordant counts (wins/losses) and an exact two-sided sign test used for
harm heuristics.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Any, Callable


@dataclass
class PairedResult:
    n: int
    wins: int
    losses: int
    diff: float
    diff_se: float
    per_item: list[dict[str, Any]]

    @property
    def base_mean(self) -> float:
        if not self.per_item:
            return 0.0
        return sum(r["base"] for r in self.per_item) / len(self.per_item)

    @property
    def p(self) -> float:
        """Two-sided normal p on the paired diff. When the diff distribution
        is degenerate (se == 0 — every item moved identically, or not at
        all), falls back to the exact sign test on discordant pairs: an
        all-wins run is the *most* significant outcome, not p = 1."""
        if self.diff_se <= 0 or math.isinf(self.diff_se):
            return sign_test_p(self.wins, self.losses)
        z = abs(self.diff) / self.diff_se
        return math.erfc(z / math.sqrt(2))


def eval_pair(
    items: list[Any],
    outcome: Callable[[Any, Any], tuple[float, float]],
) -> PairedResult:
    """Pair base vs mutated on the same items. `outcome(item, rng_slot)` returns
    (base_score, mutated_score) for the caller-chosen pairing slot."""
    rows = []
    wins = losses = 0
    for it in items:
        base, mut = outcome(it)
        d = mut - base
        if d > 0:
            wins += 1
        elif d < 0:
            losses += 1
        rows.append({"id": getattr(it, "id", str(it)), "base": base, "mut": mut, "diff": d})
    n = len(rows)
    if not n:
        return PairedResult(0, 0, 0, 0.0, 0.0, [])
    diff = sum(r["diff"] for r in rows) / n
    diffs = [r["diff"] for r in rows]
    se = 0.0
    if n > 1:
        mu = diff
        var = sum((d - mu) ** 2 for d in diffs) / (n - 1)
        se = math.sqrt(var / n)
    return PairedResult(n, wins, losses, diff, se, rows)


@dataclass
class ConditionalResult:
    """4-arm interaction estimate between artifact x and partner y.

    Per item, one draw slot drives all four arms — base (committed minus the
    pair), partner alone, x alone, joint — so the only thing that varies is
    which configs the draw sees:

        gain_i = (joint - x_solo) - (partner - base)

    The partner's own marginal is subtracted: without that term a strong
    partner would read as interaction. `solo_diff`/`joint_diff` are x's
    marginal without / with the partner (both vs the pair-free baseline)."""
    n: int
    solo_diff: float
    joint_diff: float
    gain: float
    gain_se: float
    pos: int = 0
    neg: int = 0


def eval_conditional(
    items: list[Any],
    quad: Callable[[Any], tuple[float, float, float, float]],
) -> ConditionalResult:
    """Paired 4-arm conditional estimate. `quad(item)` returns
    (base, partner_only, x_solo, joint) at the caller-chosen shared slot."""
    gains = []
    pos = neg = 0
    solo_sum = joint_sum = 0.0
    for it in items:
        b, partner, solo, joint = quad(it)
        g = (joint - solo) - (partner - b)
        gains.append(g)
        if g > 0:
            pos += 1
        elif g < 0:
            neg += 1
        solo_sum += solo - b
        joint_sum += joint - b
    n = len(gains)
    if n == 0:
        return ConditionalResult(0, 0.0, 0.0, 0.0, math.inf)
    mean = sum(gains) / n
    se = 0.0
    if n > 1:
        var = sum((g - mean) ** 2 for g in gains) / (n - 1)
        se = math.sqrt(var / n)
    return ConditionalResult(n, solo_sum / n, joint_sum / n, mean, se, pos, neg)


def sign_test_p(wins: int, losses: int) -> float:
    """Exact two-sided sign test over discordant pairs; 1.0 when there are none."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def fisher_p(p1: float, p2: float) -> float:
    """Fisher's method for combining two independent p-values."""
    stat = -2 * (math.log(max(p1, 1e-12)) + math.log(max(p2, 1e-12)))
    z = stat / 2.0
    return math.exp(-z) * (1 + z)


def tasks_needed(
    effect: float,
    base: float = 0.5,
    alpha: float = 0.05,
    power: float = 0.8,
    stat: str = "z",
) -> int:
    """Paired-item count needed to resolve an effect of size `effect` on a
    Bernoulli outcome around base rate `base`, at two-sided alpha, with the
    given power. `stat='z'` mirrors the gate's paired z-test; 'sign' uses the
    McNemar discordant-pair approximation."""
    effect = abs(effect)
    if effect <= 0:
        return 0
    p_b = min(max(base, 0.0), 1.0)
    p_m = min(max(p_b + effect, 0.0), 1.0)
    za = NormalDist().inv_cdf(1 - alpha / 2)
    zb = NormalDist().inv_cdf(power)
    if stat == "sign":
        p10 = (1 - p_b) * p_m
        p01 = p_b * (1 - p_m)
        num = (za + zb) ** 2 * (p10 + p01)
        den = (p10 - p01) ** 2
        return math.ceil(num / den) if den > 0 else 0
    var = p_b * (1 - p_b) + p_m * (1 - p_m)
    if var <= 0:
        return 0
    return math.ceil(((za + zb) ** 2 * var) / (effect**2))
