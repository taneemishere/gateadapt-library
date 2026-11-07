"""Live substrate: ChatClient wire format + rate limiting (mocked opener),
live_run contract, scorers, and the llm_generate synthesis callable.

No real network anywhere, every transport is a stub.
"""

import json
import time

import pytest

from gateadapt import Task
from gateadapt.live import (
    contains,
    exact_match,
    live_run,
    llm_generate,
    llm_judge,
    predicate,
)
from gateadapt.llm import ChatClient, NvidiaNIMClient, RateLimiter, transient_error


class FakeHTTPResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


class FakeOpener:
    """Returns canned responses; records every request for inspection."""

    def __init__(self, contents):
        self.contents = list(contents)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        content = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
        return FakeHTTPResponse(
            {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )


def make_client(contents, **kw):
    return ChatClient(
        api_key="k",
        model="m",
        url="http://x/v1/chat/completions",
        opener=FakeOpener(contents),
        **kw,
    )


def task(**data):
    return Task(id="t1", tags={"type": "answer"}, data=data)


class TestChatClient:
    def test_request_wire_format(self):
        c = make_client(["hi"], temperature=0.2, seed=7, max_tokens=64)
        assert c.chat([{"role": "user", "content": "hello"}]) == "hi"
        body = json.loads(c.opener.requests[0].data.decode())
        assert body["model"] == "m"
        assert body["temperature"] == 0.2
        assert body["seed"] == 7
        assert body["max_tokens"] == 64
        assert body["messages"][0]["content"] == "hello"
        assert c.opener.requests[0].get_header("Authorization") == "Bearer k"

    def test_per_call_overrides(self):
        c = make_client(["ok"], temperature=0.0)
        c.chat([{"role": "user", "content": "x"}], model="other", temperature=0.9)
        body = json.loads(c.opener.requests[0].data.decode())
        assert body["model"] == "other"
        assert body["temperature"] == 0.9

    def test_retry_on_transient_then_success(self):
        class Flaky:
            def __init__(self):
                self.calls = 0

            def __call__(self, request, timeout=None):
                self.calls += 1
                if self.calls < 3:
                    raise OSError("connection reset")
                return FakeHTTPResponse({"choices": [{"message": {"content": "recovered"}}]})

        c = ChatClient("k", "m", "http://x", opener=Flaky(), retries=5)
        orig_sleep, calls = time.sleep, []
        time.sleep = lambda s: calls.append(s)
        try:
            assert c.chat([{"role": "user", "content": "x"}]) == "recovered"
        finally:
            time.sleep = orig_sleep
        assert c.opener.calls == 3

    def test_permanent_4xx_no_retry(self):
        class Fails:
            def __init__(self):
                self.calls = 0

            def __call__(self, request, timeout=None):
                self.calls += 1
                import urllib.error, io

                raise urllib.error.HTTPError(
                    "u", 400, "bad request", {}, io.BytesIO(b"bad")
                )

        c = ChatClient("k", "m", "http://x", opener=Fails())
        with pytest.raises(RuntimeError, match="HTTP 400"):
            c.chat([{"role": "user", "content": "x"}])
        assert c.opener.calls == 1  # no retries on permanent errors


class TestRateLimiter:
    def test_caps_calls_per_minute(self):
        rl = RateLimiter(max_rpm=60)  # 1/s, measurable without a long wait
        t0 = time.monotonic()
        for _ in range(3):
            rl.wait()
        assert time.monotonic() - t0 >= 2.0  # 3rd call ≥ 2 intervals out

    def test_default_under_provider_cap(self):
        assert RateLimiter().max_rpm <= 40


class TestLiveRun:
    def test_messages_and_scorer(self):
        seen = {}

        class LLM:
            def chat(self, messages, **overrides):
                seen["messages"] = messages
                seen["overrides"] = overrides
                return "Paris"

        run = live_run(LLM(), exact_match())
        t = task(question="capital of France?", gold="Paris", context="Geo facts.")
        score = run(t, {"system_prompt": "Be terse", "prompt_suffix": "One word."})
        assert score == 1.0
        sys_msg, user_msg = seen["messages"]
        assert sys_msg == {"role": "system", "content": "Be terse"}
        assert "capital of France?" in user_msg["content"]
        assert "Geo facts." in user_msg["content"]
        assert "One word." in user_msg["content"]

    def test_config_overrides_reach_wire(self):
        seen = {}

        class LLM:
            def chat(self, messages, **overrides):
                seen.update(overrides)
                return "x"

        run = live_run(LLM(), exact_match())
        run(
            task(question="q", gold="x"),
            {"temperature": 0.7, "max_tokens": 8, "model": "other", "irrelevant": 1},
        )
        assert seen == {"temperature": 0.7, "max_tokens": 8, "model": "other"}

    def test_mutation_patch_changes_episode(self):
        """The core property: the same task scores differently under the patch,
        which is what a Gate measurement on a live agent actually is."""
        from gateadapt.mutation import apply_patch

        class LLM:
            def chat(self, messages, **o):
                return "REFUSE" if "One word" in messages[1]["content"] else "maybe"

        run = live_run(LLM(), contains())
        t = task(question="unknown", gold="REFUSE")
        base = run(t, {})
        patched = run(t, apply_patch({}, {"prompt_suffix": "One word only."}))
        assert base == 0.0 and patched == 1.0


class TestScorers:
    def test_exact_match_gold_list(self):
        s = exact_match()
        t = task(gold=["yes", "y"])
        assert s(t, "Yes") == 1.0 and s(t, "no") == 0.0

    def test_contains(self):
        s = contains()
        t = task(gold="Paris")
        assert s(t, "The capital is Paris, France.") == 1.0
        assert s(t, "London") == 0.0

    def test_predicate(self):
        s = predicate(lambda task, text: len(text) < 20)
        t = task()
        assert s(t, "short") == 1.0 and s(t, "x" * 30) == 0.0

    def test_llm_judge(self):
        class Judge:
            def chat(self, messages, **o):
                return "CORRECT" if "right" in messages[0]["content"] else "INCORRECT"

        s = llm_judge(Judge())
        t = task(question="q", gold="g")
        assert s(t, "the right answer") == 1.0
        assert s(t, "wrong") == 0.0


class TestLLMGenerate:
    def test_mints_and_filters(self):
        minted = [
            {"id": "s1", "tags": {"type": "refusal"}, "question": "q1", "gold": "REFUSE"},
            {"id": "s2", "tags": {"type": "answer"}, "question": "q2", "gold": "x"},
            {"id": "s3", "tags": {"type": "refusal"}, "question": "q3", "gold": "REFUSE"},
        ]

        class LLM:
            def chat(self, messages, **o):
                return json.dumps(minted)

        import random

        gen = llm_generate(LLM(), describe="refusal tasks")
        out = gen(lambda t: t.tags.get("type") == "refusal", 2, random.Random(0))
        assert [t.id for t in out] == ["s1", "s3"]
        assert all(t.data["gold"] == "REFUSE" for t in out)

    def test_describe_callable_sees_region_only(self):
        captured = []

        class LLM:
            def chat(self, messages, **o):
                return "[]"

        def describe(region_fn):
            captured.append(region_fn)
            return "whatever"

        gen = llm_generate(LLM(), describe=describe)
        region = lambda t: True
        gen(region, 1, __import__("random").Random(0))
        assert captured[0] is region  # predicate itself, no mutation info

    def test_bad_json_retries_then_returns_partial(self):
        class LLM:
            calls = 0

            def chat(self, messages, **o):
                type(self).calls += 1
                if type(self).calls == 1:
                    return "not json at all"
                return '[{"id": "ok", "tags": {}, "question": "q", "gold": "g"}]'

        gen = llm_generate(LLM(), describe="x", max_batches=3)
        out = gen(lambda t: True, 3, __import__("random").Random(0))
        assert len(out) == 1 and out[0].id == "ok"


class TestNIMClient:
    def test_requires_key(self):
        with pytest.raises(RuntimeError, match="NVIDIA_API_KEY"):
            NvidiaNIMClient(api_key="")

    def test_endpoint_and_default_model(self):
        c = NvidiaNIMClient(api_key="k")
        assert "nvidia.com" in c.url and "nemotron" in c.model


class TestTransient:
    def test_oserror_is_transient(self):
        assert transient_error(OSError("x"))

    def test_429_and_5xx_transient(self):
        assert transient_error(RuntimeError("HTTP 429 rate limited"))
        assert transient_error(RuntimeError("HTTP 503 down"))
        assert not transient_error(RuntimeError("HTTP 400 bad"))
