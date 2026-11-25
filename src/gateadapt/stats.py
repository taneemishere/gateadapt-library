"""Paired measurement + decision statistics.

The pairing is the point: every in-scope item produces a (base, mutated)
score pair from the same task, and the diff distribution, not the pass
rate, is what the test consumes. Binary scores decide by exact McNemar
(the two-sided sign test on discordant pairs); continuous scores use the
paired z-test, with the sign test as the small-count fallback.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from statistics import NormalDist
from typing import Any

# below this many discordant pairs the paired z-test is anti-conservative
_MIN_DISCORDANT = 8


@dataclass
class PairedResult:
    n: int
    wins: int
    losses: int
    diff: float
    diff_se: float
    per_item: list[dict[str, Any]]
    # every score in {0, 1}: the panel is a 2x2 table and exact McNemar
    # (the sign test on discordant pairs) is the valid test at any size
    binary: bool = False

    @property
    def base_mean(self) -> float:
        if not self.per_item:
            return 0.0
        return sum(r["base"] for r in self.per_item) / len(self.per_item)

    @property
    def robust_se(self) -> float:
        """The diff se, floored by a smoothed estimate that adds one
        pseudo-pair on each side. A panel with no discordant pairs reports
        se 0, which would read as certainty; the floor keeps comparisons
        and intervals honest there. Scores live in [0, 1], so |diff| <= 1
        per item and the floor is never loose by more than that bound."""
        if self.n == 0:
            return float("inf")
        m = self.n + 2
        disc = self.wins + self.losses + 2
        var = disc / m - ((self.wins - self.losses) / m) ** 2
        return max(self.diff_se, math.sqrt(max(var, 0.0) / m))

    def ci(self, level: float = 0.95) -> tuple[float, float]:
        """Two-sided interval on the paired diff at `level`, clipped to the
        [-1, 1] range a [0, 1] score allows.

        Binary panels take the endpoint facing 0 from the exact
        Clopper-Pearson interval on the discordant split (the interval the
        sign test inverts), so the CI excludes 0 exactly when the exact test
        rejects at 1 - level. That interval is conditional on the discordant
        count, which pins its far endpoint to the observed diff, so the far
        side takes the wider of it and the normal interval. With no
        discordant pairs the rule of three bounds the discordance rate.
        Continuous scores use a normal interval on robust_se."""
        if self.n == 0:
            return (-1.0, 1.0)
        z = NormalDist().inv_cdf(0.5 + level / 2)
        half = z * self.robust_se
        n_lo, n_hi = self.diff - half, self.diff + half
        if self.binary:
            d = self.wins + self.losses
            if d == 0:
                b = min(1.0, 3.0 / self.n)
                return (max(-b, n_lo, -1.0), min(b, n_hi, 1.0))
            cp_lo, cp_hi = clopper_pearson(self.wins, d, level)
            e_lo, e_hi = d / self.n * (2 * cp_lo - 1), d / self.n * (2 * cp_hi - 1)
            if self.diff > 0:
                n_lo, n_hi = e_lo, max(e_hi, n_hi)
            elif self.diff < 0:
                n_lo, n_hi = min(e_lo, n_lo), e_hi
            else:
                n_lo, n_hi = min(e_lo, n_lo), max(e_hi, n_hi)
        return (max(-1.0, n_lo), min(1.0, n_hi))

    @property
    def decides_by_sign_test(self) -> bool:
        """Whether `p` is the exact sign test: always for binary scores
        (the z-test drifts above nominal on discrete diffs, ~6% at a 5%
        alpha on 100 items), otherwise below _MIN_DISCORDANT discordant
        pairs or on a degenerate diff distribution."""
        discordant = self.wins + self.losses
        return (
            self.binary
            or self.diff_se <= 0
            or math.isinf(self.diff_se)
            or discordant < _MIN_DISCORDANT
        )

    @property
    def p(self) -> float:
        """Two-sided normal p on the paired diff. With fewer than
        _MIN_DISCORDANT discordant pairs the z tail reads anti-
        conservative (a handful of wins or losses looks like a tight se),
        so the exact sign test, the McNemar statistic, is the bound. The
        same fallback covers a degenerate diff distribution."""
        if self.decides_by_sign_test:
            return sign_test_p(self.wins, self.losses)
        z = abs(self.diff) / self.diff_se
        return math.erfc(z / math.sqrt(2))


def eval_pair(
    items: list[Any],
    outcome: Callable[[Any, Any], tuple[float, float]],
) -> PairedResult:
    """Pair base vs mutated on the same items. `outcome(item)` returns
    (base_score, mutated_score); the caller chooses the shared draw slot."""
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
    binary = all(r["base"] in (0.0, 1.0) and r["mut"] in (0.0, 1.0) for r in rows)
    return PairedResult(n, wins, losses, diff, se, rows, binary)


@dataclass
class ConditionalResult:
    """4-arm interaction estimate between artifact x and partner y.

    Per item, one draw slot drives all four arms, base (committed minus the
    pair), partner alone, x alone, joint, so the only thing that varies is
    which configs the draw sees:

        gain_i = (joint - x_solo) - (partner - base)

    The partner's own marginal is subtracted: without that term a strong
    partner would read as interaction. `solo_diff`/`joint_diff` are x's
    marginal without / with the partner (both vs the pair-free baseline).

    `expected` is the naive additive expectation base + x_marginal +
    y_marginal, `joint_mean` the realized joint. `shortfall` is the gap
    between the joint and the expectation the score ceiling permits. A
    saturated pair lands on the cap and shows ~0; real cancellation stays
    below it. Mere subadditivity, which every bounded score shows once
    arms saturate, is not itself a conflict."""
    n: int
    solo_diff: float
    joint_diff: float
    gain: float
    gain_se: float
    pos: int = 0
    neg: int = 0
    joint_mean: float = 0.0
    expected: float = 0.0

    @property
    def shortfall(self) -> float:
        return min(1.0, self.expected) - self.joint_mean


def eval_conditional(
    items: list[Any],
    quad: Callable[[Any], tuple[float, float, float, float]],
) -> ConditionalResult:
    """Paired 4-arm conditional estimate. `quad(item)` returns
    (base, partner_only, x_solo, joint) at the caller-chosen shared slot."""
    gains = []
    pos = neg = 0
    solo_sum = joint_sum = 0.0
    partner_sum = joint_raw = 0.0
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
        partner_sum += partner
        joint_raw += joint
    n = len(gains)
    if n == 0:
        return ConditionalResult(0, 0.0, 0.0, 0.0, math.inf)
    mean = sum(gains) / n
    se = 0.0
    if n > 1:
        var = sum((g - mean) ** 2 for g in gains) / (n - 1)
        se = math.sqrt(var / n)
    return ConditionalResult(
        n, solo_sum / n, joint_sum / n, mean, se, pos, neg,
        joint_raw / n, (solo_sum + partner_sum) / n,
    )


def sign_test_p(wins: int, losses: int) -> float:
    """Exact two-sided sign test over discordant pairs; 1.0 when there are none."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def sign_needed(wins: int, losses: int, alpha: float = 0.05) -> int:
    """Smallest number of additional decisive pairs for the exact sign test
    to pass at two-sided `alpha`, assuming the new items all land on the
    observed majority side. 0 when the current count already decides."""
    if not 0 < alpha < 1:
        return 0
    hi, lo = max(wins, losses), min(wins, losses)
    k = 0
    while sign_test_p(hi + k, lo) >= alpha:
        k += 1
    return k


def _binom_tail_ge(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), summed in log space."""
    if k <= 0:
        return 1.0
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    lp, lq = math.log(p), math.log1p(-p)
    total = 0.0
    for i in range(k, n + 1):
        total += math.exp(
            math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq
        )
    return min(1.0, total)


def clopper_pearson(k: int, n: int, level: float = 0.95) -> tuple[float, float]:
    """Exact two-sided binomial interval for k successes in n trials."""
    a = (1 - level) / 2

    def solve(f) -> float:
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if f(mid):
                hi = mid
            else:
                lo = mid
        return (lo + hi) / 2

    lower = 0.0 if k == 0 else solve(lambda q: _binom_tail_ge(k, n, q) >= a)
    upper = 1.0 if k == n else solve(lambda q: 1 - _binom_tail_ge(k + 1, n, q) <= a)
    return lower, upper


def below_target_p(measure: PairedResult, target: float) -> float:
    """One-sided p for H0 `true diff >= target` against `true diff < target`.
    Small means the measurement sits credibly below the target."""
    if measure.n == 0:
        return 1.0
    z = (target - measure.diff) / measure.robust_se
    return 1 - NormalDist().cdf(z)


def gap_p(a: PairedResult, b: PairedResult) -> float:
    """One-sided p that `a`'s effect exceeds `b`'s (independent panels).
    Small means the two regions really differ, not just by noise."""
    if a.n == 0 or b.n == 0:
        return 1.0
    se = math.sqrt(a.robust_se**2 + b.robust_se**2)
    return 1 - NormalDist().cdf((a.diff - b.diff) / se)


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
