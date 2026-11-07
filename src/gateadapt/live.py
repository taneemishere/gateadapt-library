"""Live substrate: turn an LLM client into `run(task, config) -> score`.

The gate never sees the wire, it sees the same contract as the toy agent:
a task, an effective config, a score. Mutation patches ARE the harness
edits. `patch: {"system_prompt": "..."}` or `patch: {"prompt_suffix": "..."}`
changes the episode the model actually runs, which is what
measurement-gated admission on a real agent means in practice.

Per-call config knobs (merged into each request): `system_prompt`,
`prompt_suffix`, `temperature`, `max_tokens`, `seed`, `model`. A mutation
that flips `model` measures a model change through the same gate.

Task shape (the same JSONL Task as everywhere else):
    {"id": "...", "tags": {"type": "refusal"},
     "question": "...", "context": "...", "gold": "REFUSE"}
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from .llm import client_identity
from .models import Task

_DEFAULT_SYSTEM = "You are a precise agent. Answer the task correctly."


def _render_user(task: Task, suffix: str) -> str:
    parts = []
    context = task.data.get("context")
    if context:
        parts.append(f"Context:\n{context}")
    parts.append(f"Task:\n{task.data.get('question', '')}")
    if suffix:
        parts.append(suffix)
    return "\n\n".join(parts)


def live_run(
    llm: Any,
    scorer: Callable[[Task, str], float],
    default_system: str = _DEFAULT_SYSTEM,
) -> Callable[[Task, dict], float]:
    """`run(task, config) -> score` backed by an LLM client.

    `llm` is anything with `.chat(messages, **overrides) -> str` (see
    gateadapt.llm.ChatClient). `scorer(task, response_text)` returns the
    episode score, supply task semantics (exact match, rubric judge, ...).
    """

    def run(task: Task, config: dict) -> float:
        system = config.get("system_prompt", default_system)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": _render_user(task, config.get("prompt_suffix", ""))},
        ]
        overrides = {
            k: config[k]
            for k in ("temperature", "max_tokens", "seed", "model")
            if k in config
        }
        text = llm.chat(messages, **overrides)
        return float(scorer(task, text))

    # ambient client params are part of the episode's identity, the run
    # name feeds the episode-cache key
    run.__qualname__ = f"live_run[{client_identity(llm)}]"
    return run


# ------------------------------------------------------------------- scorers


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def exact_match(field: str = "gold") -> Callable[[Task, str], float]:
    """1.0 iff the normalized response equals the gold answer. `gold` may be
    a string or a list of acceptable answers."""

    def scorer(task: Task, text: str) -> float:
        gold = task.data.get(field, "")
        answers = gold if isinstance(gold, list) else [gold]
        return float(any(_norm(text) == _norm(a) for a in answers))

    return scorer


def contains(field: str = "gold") -> Callable[[Task, str], float]:
    """1.0 iff the gold string appears verbatim (normalized) in the response."""

    def scorer(task: Task, text: str) -> float:
        gold = _norm(task.data.get(field, ""))
        return float(bool(gold) and gold in _norm(text))

    return scorer


def predicate(fn: Callable[[Task, str], bool]) -> Callable[[Task, str], float]:
    """Wrap any (task, response) -> bool into a scorer, e.g. refusal checks,
    format checks, regex suites. You own the semantics; the gate owns the
    measurement."""
    return lambda task, text: float(bool(fn(task, text)))


def llm_judge(
    llm: Any,
    rubric: str = "Return CORRECT iff the response answers the task per the gold reference; otherwise INCORRECT.",
) -> Callable[[Task, str], float]:
    """A second model call judges each response. Honest but doubles episode
    cost, cache aggressively (the episode cache already dedupes)."""

    def scorer(task: Task, text: str) -> float:
        prompt = (
            f"Task:\n{task.data.get('question', '')}\n\n"
            f"Gold reference:\n{task.data.get('gold', '')}\n\n"
            f"Response:\n{text}\n\n{rubric}"
        )
        verdict = llm.chat([{"role": "user", "content": prompt}])
        return float("correct" in _norm(verdict) and "incorrect" not in _norm(verdict))

    return scorer


# ------------------------------------------------- G2 item synthesis (live)


def llm_generate(
    llm: Any,
    describe: Callable[[Callable[[Task], bool]], str] | str,
    example: dict[str, Any] | None = None,
    id_prefix: str = "syn",
    max_batches: int = 3,
) -> Callable[[Callable[[Task], bool], int, Any], list[Task]]:
    """A `generate(region_fn, n, rng)` callable backed by the model.

    Direction-blind by contract: `describe` is a string or a callable
    mapping the region predicate to a *region description*, the mutation,
    its payload, and its effect sign never reach the prompt. Minted items
    are re-filtered through `region_fn` by the gate regardless.
    """

    def generate(region_fn, n: int, rng) -> list[Task]:
        text = describe(region_fn) if callable(describe) else describe
        out: list[Task] = []
        seen: set[str] = set()
        for _ in range(max_batches):
            if len(out) >= n:
                break
            ask = n - len(out)
            prompt = (
                f"Mint {ask} benchmark tasks as a JSON array. Each element: "
                f'{{"id": "...", "tags": {{...}}, "question": "...", "context": "...", '
                f'"gold": "..."}}.\nRegion description: {text}\n'
                + (f"Shape example: {json.dumps(example)}\n" if example else "")
                + "Return ONLY the JSON array."
            )
            raw = llm.chat([{"role": "user", "content": prompt}])
            try:
                rows = json.loads(raw[raw.find("[") : raw.rfind("]") + 1])
            except (json.JSONDecodeError, ValueError):
                continue
            for i, row in enumerate(rows):
                t = Task(
                    id=str(row.get("id", f"{id_prefix}-{rng.randrange(10**6)}")),
                    tags=dict(row.get("tags") or {}),
                    data={
                        k: v
                        for k, v in row.items()
                        if k not in ("id", "tags")
                    },
                )
                # models repeat ids across batches, dedupe so the test pool
                # can't double-count a minted item
                if region_fn(t) and t.id not in seen:
                    seen.add(t.id)
                    out.append(t)
        return out[:n]

    return generate
