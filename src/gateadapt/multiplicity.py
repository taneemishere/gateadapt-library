"""Per-family look budget with refund-on-decision.

Every gated look spends from a shared per-family pool priced in equal
shares (wealth / looks per look). A look runs only while a full share
remains; a decided look (COMMIT or REJECT) refunds its share and an
unresolved look forfeits it, so a family that churns inconclusively drains
its budget in `looks` unresolved looks.

This is not alpha-investing (Foster and Stine) and not SAFFRON: neither
the payout rule nor adaptive discarding is implemented. Its guarantee is
the one it can prove. A null look decides with probability at most
alpha = wealth/looks, and spent wealth only grows on unresolved looks, so a
family's expected number of false decisions satisfies
E[D] <= looks * alpha / (1 - alpha), given valid conditional p-values.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AlphaPool:
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


def __getattr__(name: str):
    # the pre-rename alias still imports, with a warning, until 1.0
    if name == "SaffronPool":
        import warnings

        warnings.warn(
            "SaffronPool is deprecated, use AlphaPool (the pool is not SAFFRON)",
            DeprecationWarning,
            stacklevel=2,
        )
        return AlphaPool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
