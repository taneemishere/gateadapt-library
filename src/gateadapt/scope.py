"""Deterministic scope predicate grammar.

A scope is an AST over task features, evaluated without any model call:

    ("all",)
    ("eq", "tags.type", "refusal")
    ("neq", "question_kind", "lookup")
    ("in", "domain", ("acme", "nimbus"))
    ("lt" | "le" | "gt" | "ge", "num_docs", 5)
    ("and", t1, t2, ...)
    ("or", t1, t2, ...)
    ("not", t1)

Text DSL (what a mutation file declares):

    all
    tags.type == "refusal"
    and(tags.domain == "acme", or(tags.kind == "lookup", num_docs >= 4))
    not(tags.needs_refusal == true)
    tags.difficulty in ["easy", "medium"]

Field names may be dotted paths resolved against the task's feature dict
("tags.type" -> features["tags"]["type"]).

evaluate() is pure: same AST + same features -> same answer. That purity is
what makes the scope claim checkable.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .models import FeatureSpec

_OPS = {"eq", "neq", "in", "lt", "le", "gt", "ge"}
_NUMERIC = (int, float)


def _get(features: dict[str, Any], field: str) -> Any:
    """Dotted-path lookup: 'tags.type' -> features['tags']['type']."""
    if field in features:
        return features[field]
    cur: Any = features
    for part in field.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def evaluate(node: Any, features: dict[str, Any]) -> bool:
    """Deterministically evaluate a scope AST against item features."""
    head = node[0]
    if head == "all":
        return True
    if head == "and":
        return all(evaluate(t, features) for t in node[1:])
    if head == "or":
        return any(evaluate(t, features) for t in node[1:])
    if head == "not":
        return not evaluate(node[1], features)
    value = _get(features, node[1])
    if head == "eq":
        return value == node[2]
    if head == "neq":
        return value != node[2]
    if head == "in":
        return value in node[2]
    if value is None or isinstance(value, (bool, str)):
        return False
    if head == "lt":
        return value < node[2]
    if head == "le":
        return value <= node[2]
    if head == "gt":
        return value > node[2]
    if head == "ge":
        return value >= node[2]
    raise ValueError(f"unknown scope op {head!r}")


def _type_ok(value: Any, ftype: type) -> bool:
    """Feature values: numeric fields accept int/float (never bool);
    everything else must match exactly."""
    if ftype in _NUMERIC and ftype is not bool:
        return isinstance(value, _NUMERIC) and not isinstance(value, bool)
    return isinstance(value, ftype)


def validate(node: Any, spec: FeatureSpec) -> list[str]:
    """Structural validation for G0. Returns violations (empty = ok)."""
    errors: list[str] = []
    if not isinstance(node, tuple) or not node:
        return ["scope node is not a non-empty tuple"]
    head = node[0]
    if head == "all":
        if len(node) != 1:
            errors.append("'all' takes no arguments")
        return errors
    if head in ("and", "or"):
        if len(node) < 3:
            errors.append(f"'{head}' needs at least two children")
        for child in node[1:]:
            errors.extend(validate(child, spec))
        return errors
    if head == "not":
        if len(node) != 2:
            errors.append("'not' takes exactly one child")
            return errors
        return errors + validate(node[1], spec)
    if head not in _OPS:
        return [f"unknown scope op {head!r}"]
    if len(node) != 3:
        errors.append(f"'{head}' takes a field and a value")
        return errors
    _, field_name, value = node
    ftype = spec.fields.get(field_name)
    if ftype is None:
        errors.append(f"unknown feature {field_name!r}")
        return errors
    if head == "in":
        if not isinstance(value, tuple) or not value:
            errors.append("'in' needs a non-empty tuple of values")
            return errors
        if not all(_type_ok(v, ftype) for v in value):
            errors.append(f"'in' values must all match {ftype.__name__}")
        return errors
    if head in ("lt", "le", "gt", "ge") and ftype not in _NUMERIC:
        errors.append(f"ordered comparison '{head}' on non-numeric feature {field_name!r}")
        return errors
    if not _type_ok(value, ftype):
        errors.append(
            f"feature {field_name!r} is {ftype.__name__}, got {type(value).__name__}"
        )
    return errors


def to_jsonable(node: Any) -> Any:
    if isinstance(node, tuple):
        return [to_jsonable(c) for c in node]
    return node


def from_jsonable(node: Any) -> Any:
    if isinstance(node, list):
        return tuple(from_jsonable(c) for c in node)
    return node


_TOKEN = re.compile(
    r"\s*(\(|\)|\[|\]|,|==|!=|<=?|>=?|'[^']*'|\"[^\"]*\"|[A-Za-z_][\w.]*|-?\d+(?:\.\d+)?|true|false)"
)


def _tokens(text: str) -> list[str]:
    out, pos = [], 0
    for m in _TOKEN.finditer(text):
        if m.start() != pos:
            raise ValueError(f"cannot parse scope near {text[pos:]!r}")
        out.append(m.group(1))
        pos = m.end()
    if text[pos:].strip():
        raise ValueError(f"cannot parse scope near {text[pos:]!r}")
    return out


def _atom(tok: str) -> Any:
    if tok == "true":
        return True
    if tok == "false":
        return False
    if tok.startswith(('"', "'")):
        return tok[1:-1]
    try:
        return int(tok)
    except ValueError:
        return float(tok)


class _Parser:
    def __init__(self, toks: list[str]):
        self.toks = toks
        self.i = 0

    def peek(self) -> str | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def next(self) -> str:
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def parse(self) -> Any:
        tok = self.next()
        if tok in ("and", "or", "not"):
            return self._compound(tok)
        if tok == "all":
            return ("all",)
        field_name = tok
        op = self.next()
        if op == "[":
            return self._in(field_name, already_open=True)
        opmap = {"==": "eq", "!=": "neq", "<": "lt", "<=": "le", ">": "gt", ">=": "ge", "in": "in"}
        if op not in opmap:
            raise ValueError(f"expected comparison op, got {op!r}")
        if opmap[op] == "in":
            return self._in(field_name)
        return (opmap[op], field_name, _atom(self.next()))

    def _in(self, field_name: str, already_open: bool = False) -> Any:
        if not already_open and self.next() != "[":
            raise ValueError("'in' needs [a, b, ...]")
        values = []
        while True:
            values.append(_atom(self.next()))
            sep = self.next()
            if sep == "]":
                break
            if sep != ",":
                raise ValueError(f"expected ',' or ']' in 'in' list, got {sep!r}")
        return ("in", field_name, tuple(values))

    def _compound(self, head: str) -> Any:
        if self.next() != "(":
            raise ValueError(f"'{head}' needs ( ... )")
        children = [self.parse()]
        while True:
            sep = self.next()
            if sep == ")":
                break
            if sep != ",":
                raise ValueError(f"expected ',' or ')' in '{head}', got {sep!r}")
            children.append(self.parse())
        if head == "not":
            if len(children) != 1:
                raise ValueError("'not' takes exactly one child")
            return ("not", children[0])
        return (head, *children)


def parse(text: str) -> Any:
    """Parse the text DSL into a scope AST."""
    toks = _tokens(text)
    if not toks:
        raise ValueError("empty scope")
    parser = _Parser(toks)
    ast = parser.parse()
    if parser.i < len(toks):
        raise ValueError("trailing tokens in scope")
    return ast


def dumps(ast: Any) -> str:
    """Deterministic serialization for artifact identity/lineage."""
    return json.dumps(to_jsonable(ast), sort_keys=False, separators=(",", ":"))


def infer_spec(tasks: list) -> FeatureSpec:
    """Infer the feature vocabulary from a task set: 'tags.<path>' keys plus
    scalar top-level fields. First non-None observed value sets the type;
    int/float unify as numbers."""
    fields: dict[str, type] = {}

    def walk(prefix: str, d: dict[str, Any]) -> None:
        for k, v in d.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                walk(path, v)
            elif v is not None and path not in fields:
                fields[path] = type(v)

    for t in tasks:
        walk("tags", t.tags)
        for k, v in t.data.items():
            if isinstance(v, dict):
                walk(k, v)
            elif v is not None and k not in fields:
                fields[k] = type(v)
    return FeatureSpec(fields=fields)
