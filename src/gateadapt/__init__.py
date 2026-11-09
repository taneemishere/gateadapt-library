"""GateAdapt, CI for agent changes.

User supplies:
  - run(task, config) -> score        (their agent)
  - tasks with tags, split into dev / probe / audit decks
  - a Mutation: id, kind, patch, deterministic scope

Gate answers COMMIT, REJECT, ABSTAIN_WITH_RECORD, or SCOPE_CORRECT,
each backed by paired evidence on the declared region only.
"""

from .decks import Deck, HonestyViolation, dump_tasks, load_tasks
from .gate import Gate, g0_check
from .ledger import committed_from_ledger, read_ledger
from .live import contains, exact_match, live_run, llm_generate, llm_judge, predicate
from .multiplicity import AlphaPool, SaffronPool
from .models import (
    ArtifactKind,
    Decision,
    DeckRole,
    FeatureSpec,
    Mutation,
    Task,
    Verdict,
)
from .scope import evaluate, infer_spec, parse, validate
from .stats import ConditionalResult, PairedResult, eval_pair, sign_test_p, tasks_needed

__version__ = "0.4.0"

__all__ = [
    "Gate",
    "Mutation",
    "Task",
    "Verdict",
    "Decision",
    "ArtifactKind",
    "DeckRole",
    "Deck",
    "FeatureSpec",
    "HonestyViolation",
    "PairedResult",
    "ConditionalResult",
    "AlphaPool",
    "SaffronPool",
    "load_tasks",
    "dump_tasks",
    "g0_check",
    "evaluate",
    "parse",
    "validate",
    "infer_spec",
    "eval_pair",
    "sign_test_p",
    "tasks_needed",
    "read_ledger",
    "committed_from_ledger",
    "live_run",
    "exact_match",
    "contains",
    "predicate",
    "llm_judge",
    "llm_generate",
]
