"""SAFFRON-style alpha-investing wealth pool.

Every gated look spends wealth from a shared per-family pool priced in equal
quarters (wealth / looks per look). Wealth is the only thing that gates: a
look runs only while a full quarter remains, a decision, COMMIT or REJECT,
refunds its spent alpha, and an unresolved look forfeits it. That makes
indecision costly: a family of decisive members keeps testing (each decided
look returns its quarter), while a family that churns inconclusively drains
its budget in four unresolved looks. Total wealth *spent* stays bounded at
the pool size regardless of test count, the alpha-investing property that
keeps sequential peeping honest.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SaffronPool:
    """Shared per-family test budget.

    `refund_decided` is the refund-on-decision switch: when True a decided
    look returns its spent alpha (decisive tests keep the family alive, only
    indecision drains it); when False every look is consumed regardless.
    """

    wealth: float = 0.10
    looks: int = 4
    refund_decided: bool = True
    _spent: float = field(init=False, default=0.0, repr=False)
    _look_index: int = field(init=False, default=0, repr=False)

    @property
    def remaining_wealth(self) -> float:
        return self.wealth - self._spent

    @property
    def looks_left(self) -> int:
        return self.looks - self._look_index

    def allocate(self) -> float:
        """Alpha for the next look: a full equal-quarter, or nothing when the
        family's wealth can no longer fund a powered look."""
        alpha = self.wealth / self.looks
        if self.remaining_wealth < alpha - 1e-12:
            return 0.0
        self._look_index += 1
        return alpha

    def spend(self, alpha: float) -> None:
        self._spent += alpha

    def refund(self, alpha: float) -> None:
        """A decided look returns its spent alpha to the pool."""
        if not self.refund_decided:
            return
        self._spent = max(0.0, self._spent - alpha)
