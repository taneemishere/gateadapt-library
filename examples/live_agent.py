"""Live agent: an LLM behind the same `run(task, config) -> score` contract
as the toy agent. Wire it once, then `gateadapt admit` measures real
episodes — a mutation's patch (system_prompt / prompt_suffix / temperature /
model) reaches the served model per call.

Key comes from NVIDIA_API_KEY or the GATEADAPT_NVAPI_KEYFILE path
(default ~/gateadapt/nvapi_key on this machine). Rate limit
hard-capped under the provider's 40 rpm tier (default 36).

    gateadapt admit --tasks examples/tasks_live_dev.jsonl \
        --probe examples/tasks_live_probe.jsonl \
        --run examples/live_agent.py:run --base examples/config.yaml \
        --mutation examples/mutations/strict_answers.yaml \
        --draws 1 --ledger live_ledger.jsonl --episodes live_episodes.json
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from gateadapt.llm import NvidiaNIMClient, client_from_keyfile  # noqa: E402
from gateadapt.live import exact_match, live_run  # noqa: E402

_DEFAULT_KEYFILE = os.path.expanduser("~/gateadapt/nvapi_key")


def build_llm(model: str | None = None) -> NvidiaNIMClient:
    # temperature=0 keeps episodes deterministic — the eval's default;
    # ambient params are namespaced into the cache key, so overriding via
    # env never silently serves stale episodes
    temperature = float(os.environ.get("GATEADAPT_LLM_TEMPERATURE", "0"))
    if os.environ.get("NVIDIA_API_KEY"):
        return NvidiaNIMClient.from_env(
            model=model or "nvidia/nemotron-3-ultra-550b-a55b",
            max_rpm=36,
            temperature=temperature,
        )
    return client_from_keyfile(
        os.environ.get("GATEADAPT_NVAPI_KEYFILE", _DEFAULT_KEYFILE),
        model=model or "nvidia/nemotron-3-ultra-550b-a55b",
        max_rpm=36,
        temperature=temperature,
    )


llm = build_llm(os.environ.get("GATEADAPT_LLM_MODEL"))
run = live_run(llm, exact_match())


def generate(region_fn, n, rng):
    """Optional live synthesis source for G2: mint in-region tasks."""
    from gateadapt.live import llm_generate

    return llm_generate(
        llm,
        describe=lambda fn: (
            "short factual QA tasks tagged tags.type='answer' (gold = the "
            "short answer string) or unanswerable tasks tagged "
            "tags.type='refusal' (context that lacks the answer, gold "
            "exactly 'REFUSE')"
        ),
    )(region_fn, n, rng)


if __name__ == "__main__":
    # smoke: one call, prints the raw response — run manually, ~1 request
    import json
    from gateadapt.models import Task

    t = Task(id="smoke", tags={}, data={"question": "What is 2+2?", "gold": "4"})
    print(json.dumps({"score": run(t, {}), "model": llm.model}))
