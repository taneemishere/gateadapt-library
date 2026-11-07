"""Applying mutations to configs: dotted-key expansion + deep merge.

A mutation's patch is a dict merged into the effective config. Nested dicts
merge recursively; scalars overwrite. Dotted keys expand to nesting:
{"retrieval.min_score": 0.35} -> {"retrieval": {"min_score": 0.35}}.

A mutation applies to a task only where its scope fires, scoped application
is the semantics the whole admission algebra stands on.
"""

from __future__ import annotations

import copy
from typing import Any


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursive merge: dict values merge, anything else overwrites."""
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def expand_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """Expand dotted keys to nested dicts: {"a.b.c": v} -> {"a": {"b": {"c": v}}}."""
    out: dict[str, Any] = {}
    for k, v in patch.items():
        if "." in k:
            head, rest = k.split(".", 1)
            existing = out.get(head)
            node = existing if isinstance(existing, dict) else {}
            out[head] = deep_merge(node, expand_patch({rest: v}))
        else:
            out[k] = expand_patch(v) if isinstance(v, dict) else v
    return out


def apply_patch(config: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    return deep_merge(config, expand_patch(patch))


def apply_mutation(config: dict[str, Any], mutation: Any, task: Any) -> dict[str, Any]:
    """The mutation's contribution to the effective config on this task.
    Does not check scope, callers gate on fires()."""
    cfg = copy.deepcopy(config)
    if mutation.patch:
        cfg = apply_patch(cfg, mutation.patch)
    if mutation.apply is not None:
        cfg = mutation.apply(cfg, task)
    return cfg


def effective_config(base: dict[str, Any], mutations: list, task: Any) -> dict[str, Any]:
    """Committed mutations applied in order, each only where its scope fires."""
    cfg = copy.deepcopy(base)
    for m in mutations:
        if m.fires(task):
            cfg = apply_mutation(cfg, m, task)
    return cfg
