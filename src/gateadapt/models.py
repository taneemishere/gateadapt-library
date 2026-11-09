"""Core types for GateAdapt: tasks, mutations, verdicts.

A Mutation is a typed harness change (a prompt patch, tool schema, retrieval
rule, budget rule, output contract, routing rule, memory rule) carrying a
deterministic scope: either a DSL expression over task tags or a callable.
No model sits inside the scope, the region a change claims is executable
and countable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class ArtifactKind(str, Enum):
    PROMPT_PATCH = "prompt_patch"
    TOOL_SCHEMA_PATCH = "tool_schema_patch"
    RETRIEVAL_RULE = "retrieval_rule"
    BUDGET_RULE = "budget_rule"
    CONTRACT_PATCH = "contract_patch"
    ROUTING_RULE = "routing_rule"
    MEMORY_RULE = "memory_rule"


#: Kinds whose effect on an item is additive under the scope lemma: if the
#: scope does not fire, the artifact is inert, so a region measurement is
#: unbiased for the admission sign. Stateful kinds (memory_rule, budget_rule)
#: carry cross-item state and must declare a global scope.
STATELESS_KINDS = frozenset(
    k for k in ArtifactKind if k not in (ArtifactKind.MEMORY_RULE, ArtifactKind.BUDGET_RULE)
)


class Verdict(str, Enum):
    COMMIT = "commit"
    REJECT = "reject"
    ABSTAIN_WITH_RECORD = "abstain_with_record"
    SCOPE_CORRECT = "scope_correct"


class LifecycleState(str, Enum):
    """Where an artifact sits in the gate's lifecycle. The gate tracks this
    implicitly (which collection holds the mutation); `Gate.artifact_state`
    derives it explicitly for one id."""

    COMMITTED = "committed"      # admitted, in the committed set
    CONDITIONAL = "conditional"  # committed, valid only while its partner stays
    DEFERRED = "deferred"        # abstained, awaiting conditional evidence
    EXPIRED = "expired"          # deferred with the retry budget spent
    REVOKED = "revoked"          # was committed, removed by lifecycle/audit
    REJECTED = "rejected"        # decisive negative verdict this session
    UNKNOWN = "unknown"          # never proposed to this gate


class DeckRole(str, Enum):
    DEV = "dev"        # decides admission
    PROBE = "probe"    # fresh-item reserve for the pre-COMMIT transfer check
    AUDIT = "audit"    # frozen at declaration; carries reported claims
    TEST = "test"      # held out; final report only


@dataclass(frozen=True)
class Task:
    """One benchmark item. `tags` are the features scopes evaluate against;
    `data` is everything else the user's run() needs (prompt, corpus path, ...)."""
    id: str
    tags: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def features(self) -> dict[str, Any]:
        """The flat scope surface: tags nested under 'tags', plus scalar
        top-level data fields and 'id'. Dotted paths resolve nested dicts."""
        out: dict[str, Any] = {"id": self.id, "tags": dict(self.tags)}
        for k, v in self.data.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                out[k] = v
            elif isinstance(v, dict):
                out[k] = v
        return out

    @property
    def fingerprint(self) -> str:
        import hashlib

        blob = json.dumps({"id": self.id, "tags": self.tags, "data": self.data},
                          sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()[:12]


@dataclass
class Mutation:
    """A harness change under test.

    - patch: dict merged into the effective config (nested + dotted keys).
    - apply: optional callable (config, task) -> config for arbitrary edits.
    - scope: DSL string ('tags.type == "refusal"'), AST tuple, callable
      (task) -> bool, or None/'all' for global.
    """
    id: str
    kind: str = "prompt_patch"
    patch: dict[str, Any] | None = None
    scope: Any = "all"
    family: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    apply: Callable[[dict, Task], dict] | None = None
    #: declared conditional dependency: this artifact's effect only exists
    #: alongside `partner`, if the partner leaves the committed set, the
    #: lifecycle probe revokes this artifact as orphaned.
    partner: str | None = None

    def __post_init__(self) -> None:
        from .scope import parse  # local import to avoid a cycle

        if isinstance(self.scope, str):
            self.scope_text = self.scope
            self.scope_ast = parse(self.scope)
            self.scope_fn = None
        elif callable(self.scope):
            self.scope_text = None
            self.scope_ast = None
            self.scope_fn = self.scope
        else:
            self.scope_text = None
            self.scope_ast = self.scope if self.scope is not None else ("all",)
            self.scope_fn = None

    @property
    def kind_enum(self) -> ArtifactKind:
        try:
            return ArtifactKind(self.kind)
        except ValueError:
            return ArtifactKind.PROMPT_PATCH

    @property
    def family_name(self) -> str:
        return self.family or self.id

    def fires(self, task: Task) -> bool:
        from .scope import evaluate

        if self.scope_fn is not None:
            return bool(self.scope_fn(task))
        return bool(evaluate(self.scope_ast, task.features))

    def to_dict(self) -> dict[str, Any]:
        from .scope import dumps as scope_dumps

        return {
            "id": self.id,
            "kind": self.kind,
            "patch": self.patch,
            "scope": self.scope_text
            or (json.loads(scope_dumps(self.scope_ast)) if self.scope_ast is not None else None),
            "family": self.family,
            "payload": self.payload,
            "partner": self.partner,
            "serializable": self.scope_fn is None and self.apply is None,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Mutation":
        if not isinstance(d, dict) or not d.get("id"):
            raise ValueError("mutation must be a mapping with an 'id' field")
        scope = d.get("scope", "all")
        if isinstance(scope, list):
            from .scope import from_jsonable

            scope = from_jsonable(scope)
        return Mutation(
            id=d["id"],
            kind=d.get("kind", "prompt_patch"),
            patch=d.get("patch"),
            scope=scope,
            family=d.get("family", ""),
            payload=d.get("payload") or {},
            partner=d.get("partner"),
        )

    def with_scope(self, scope_ast: Any) -> "Mutation":
        from .scope import dumps as scope_dumps

        m = Mutation(
            id=self.id,
            kind=self.kind,
            patch=self.patch,
            scope=scope_ast,
            family=self.family,
            payload=self.payload,
            apply=self.apply,
            partner=self.partner,
        )
        m.scope_text = json.dumps(json.loads(scope_dumps(scope_ast)))
        return m


@dataclass
class Decision:
    artifact_id: str
    verdict: Verdict
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "verdict": self.verdict.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class FeatureSpec:
    """Declares the feature vocabulary scopes may reference. G0 rejects a
    scope mentioning an unknown field, otherwise a scope could encode an
    arbitrary lookup and the no-model-in-scope claim is vacuous."""
    fields: dict[str, type]
