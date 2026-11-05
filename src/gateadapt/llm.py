"""LLM providers: stdlib OpenAI-compatible chat clients + a rate limiter.

Kept dependency-free on purpose — the library's live arm plugs any callable
``llm.chat(messages) -> str``; these are the reference implementations for
the hosted catalogs the research used (NVIDIA NIM and any OpenAI-compatible
endpoint, including local vLLM).

Rate limiting is inside the client, not the caller: a token bucket refills
``max_rpm`` permits per minute and blocks when empty — the honest way to
stay under a provider cap without guessing per-request delays.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

NIM_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NIM_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"


class ProviderError(RuntimeError):
    """Wraps a failed HTTP call. `status=None` means a transport failure
    (URLError/timeout) — no HTTP status was ever received."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def transient_error(exc: Exception) -> bool:
    """Retryable provider failures: transport errors/timeouts and rate or
    server-side statuses. A permanent 4xx (other than 429) must not cost a
    retry round — note urllib's HTTPError IS an OSError, so we inspect the
    carried status rather than the exception type."""
    if isinstance(exc, ProviderError):
        return exc.status is None or exc.status == 429 or exc.status >= 500
    if isinstance(exc, OSError):
        return True
    message = str(exc)
    return "429" in message or "HTTP 5" in message


@dataclass(frozen=True)
class LLMResponse:
    content: str
    usage: dict[str, int]


class RateLimiter:
    """Token bucket: `max_rpm` calls per rolling minute. `wait()` sleeps the
    caller until a permit is available."""

    def __init__(self, max_rpm: int = 36) -> None:
        self.max_rpm = max(1, max_rpm)
        self._interval = 60.0 / self.max_rpm
        self._next_ok = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if now < self._next_ok:
            time.sleep(self._next_ok - now)
            now = time.monotonic()
        self._next_ok = max(now, self._next_ok) + self._interval


class ChatClient:
    """Minimal OpenAI-compatible chat client (urllib, no SDK dep).

    `opener` is injectable so tests can stub the wire; `retries` bounds the
    transient-failure backoff ladder (15s, 30s, 60s, 120s caps)."""

    def __init__(
        self,
        api_key: str,
        model: str,
        url: str,
        temperature: float | None = None,
        seed: int | None = None,
        max_tokens: int | None = None,
        timeout: int = 300,
        max_rpm: int = 36,
        retries: int = 5,
        opener: Callable[..., Any] = urllib.request.urlopen,
        label: str = "LLM",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.url = url
        self.temperature = temperature
        self.seed = seed
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.retries = retries
        self.opener = opener
        self.label = label
        self.limiter = RateLimiter(max_rpm)

    _OVERRIDES = ("model", "temperature", "seed", "max_tokens")

    def _request(
        self, messages: list[dict[str, Any]], **overrides: Any
    ) -> LLMResponse:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.seed is not None:
            payload["seed"] = self.seed
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        # Per-call overrides (from the mutation's effective config) win over
        # client defaults — this is how a `patch: {model: ...}` actually
        # changes the served endpoint for that episode.
        for k in self._OVERRIDES:
            if k in overrides:
                payload[k] = overrides[k]
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self.opener(req, timeout=self.timeout) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            raise ProviderError(
                f"{self.label} request failed with HTTP {exc.code}: {body}",
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(
                f"{self.label} request failed: {exc.reason}", status=None
            ) from exc
        choices = raw.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        content = message.get("content")
        usage = raw.get("usage") or {}
        return LLMResponse(
            content=content if isinstance(content, str) else "",
            usage={
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
            },
        )

    def chat(self, messages: list[dict[str, Any]], **overrides: Any) -> str:
        """One chat call, rate-limited, retried on transient failures.
        Per-call overrides merge into the payload (model, temperature, seed,
        max_tokens) — a mutation's `patch` reaches the wire through these."""
        self.limiter.wait()
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                return self._request(messages, **overrides).content
            except Exception as e:  # noqa: BLE001 — provider/network error
                last = e
                if not transient_error(e) or attempt == self.retries - 1:
                    raise
                time.sleep(min(120, 15 * (2**attempt)))
        raise last  # type: ignore[misc]


class NvidiaNIMClient(ChatClient):
    """NVIDIA NIM hosted catalog — OpenAI-compatible wire format."""

    def __init__(self, api_key: str, model: str = NIM_MODEL, **kwargs: Any) -> None:
        if not api_key:
            raise RuntimeError("NVIDIA_API_KEY is required for the NIM provider")
        super().__init__(
            api_key=api_key, model=model, url=NIM_URL, label="NIM", **kwargs
        )

    @classmethod
    def from_env(cls, model: str = NIM_MODEL, **kwargs: Any) -> "NvidiaNIMClient":
        return cls(
            os.environ.get("NVIDIA_API_KEY", ""),
            model=os.environ.get("NIM_MODEL", model),
            **kwargs,
        )


def client_from_keyfile(path: str, model: str = NIM_MODEL, **kwargs: Any) -> NvidiaNIMClient:
    """Convenience: read a bare API key from a file (e.g. ~/gateadapt/nvapi_key)."""
    key = open(path).read().strip()
    return NvidiaNIMClient(api_key=key, model=model, **kwargs)
