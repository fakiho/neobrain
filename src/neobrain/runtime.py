"""neoBrain native LLM runtime (SPEC §7).

The brain's own cognition runs on native OpenAI-compatible calls — OpenCode is
never used for the brain's own thoughts. Stdlib only (``urllib.request``): the
repo deliberately does not depend on ``httpx``/``openai``; the only third-party
import here is pydantic, used at the wire boundary per SPEC §10.

Boundary rule (SPEC §10): the request and the response envelopes are pydantic
models. A malformed gateway response therefore fails with a clear, actionable
error instead of an ``AttributeError`` deep inside a life-loop phase.

The API key is read from config (``NEOBRAIN_LLM_API_KEY``) or the
``LITELLM_API_KEY`` environment variable and is **never** logged or included in
an error message.
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import config

# --- errors -----------------------------------------------------------------
# These subclass the builtin RuntimeError so callers can catch either the
# specific failure or "the runtime failed" in one place.


class RuntimeConfigError(RuntimeError):
    """Missing/invalid runtime configuration. Never retried."""


class RuntimeHTTPError(RuntimeError):
    """The gateway answered with an HTTP status (possibly after retries)."""

    def __init__(self, status: int, detail: str = "") -> None:
        self.status = status
        self.detail = detail
        message = f"LLM gateway HTTP {status}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


# --- request/response envelopes (pydantic boundary) -------------------------


class ChatMessage(BaseModel):
    """One OpenAI-style chat message."""

    model_config = ConfigDict(extra="ignore")

    role: str
    content: str


class ChatRequest(BaseModel):
    """OpenAI ``POST /chat/completions`` request body."""

    model_config = ConfigDict(extra="ignore")

    model: str
    messages: list[ChatMessage]
    temperature: float = 0.7
    max_tokens: int | None = None
    response_format: dict[str, str] | None = None

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class ChatUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class ChatChoice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int = 0
    message: ChatMessage
    finish_reason: str | None = None


class ChatResponse(BaseModel):
    """OpenAI ``/chat/completions`` response body (extra fields ignored)."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    model: str | None = None
    choices: list[ChatChoice] = Field(default_factory=list)
    usage: ChatUsage | None = None

    def text(self) -> str:
        """The first choice's content; raises if the gateway returned none.

        Empty/whitespace content is treated as a failure rather than a valid
        answer: reasoning models spend their whole ``max_tokens`` budget on
        hidden ``reasoning_content`` and hand back ``content=""`` with
        ``finish_reason="length"``. Surfacing that here beats a silent no-op
        deep inside the life loop.
        """
        if not self.choices:
            raise RuntimeError("LLM gateway response contained no choices")
        choice = self.choices[0]
        content = choice.message.content
        if content is None or not content.strip():
            finish = f" (finish_reason={choice.finish_reason})" if choice.finish_reason else ""
            raise RuntimeError(
                f"LLM gateway returned empty content{finish} — the model may be a "
                "reasoning model that exhausted max_tokens before answering"
            )
        return content


# --- configuration ----------------------------------------------------------

# Model tiers are resolved through config (SPEC §7: cheap for perceive/reflect,
# stronger for act/dream). Any other value is treated as a literal model id.

# Retry policy: 3 attempts with exponential backoff between them. Retry on
# HTTP 429 / 5xx and on transport timeouts (a gateway restart is transient);
# every other 4xx fails fast.
_ATTEMPTS = 3
_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)


def _resolve_model(model: str) -> str:
    if model == "cheap":
        return config.settings.llm_model_cheap
    if model == "strong":
        return config.settings.llm_model_strong
    return model


def base_url() -> str:
    """Configured OpenAI-compatible base URL, without a trailing slash."""
    return config.settings.llm_base_url.rstrip("/")


def _api_key() -> str:
    """Bearer key from config or ``LITELLM_API_KEY``. Value is never logged."""
    key = (config.settings.llm_api_key or os.environ.get("LITELLM_API_KEY") or "").strip()
    if not key:
        raise RuntimeConfigError(
            "no LLM API key configured: set NEOBRAIN_LLM_API_KEY (or the "
            "LITELLM_API_KEY environment variable) in .env — see .env.example; "
            "neoBrain will not send an unauthenticated LLM call."
        )
    return key


def _sleep(seconds: float) -> None:
    """Backoff sleep (patched out by tests)."""
    time.sleep(seconds)


def _open(req: urllib.request.Request, timeout: float):
    """Thin ``urlopen`` wrapper so tests can inject a fake gateway."""
    return urllib.request.urlopen(req, timeout=timeout)


def _safe_detail(exc: urllib.error.HTTPError, limit: int = 300) -> str:
    """Best-effort response body for an error message (never the request/key)."""
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - the body is only diagnostic
        return ""
    raw = " ".join(raw.split())
    return raw[:limit]


def _is_retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status < 600


# --- the call ---------------------------------------------------------------


def chat(
    messages: list[dict[str, str]],
    *,
    model: str = "cheap",
    temperature: float = 0.7,
    max_tokens: int | None = None,
    json_mode: bool = False,
    timeout: float = 120.0,
) -> str:
    """One OpenAI-compatible chat completion; returns the assistant text.

    ``model`` is a tier name (``cheap`` / ``strong``) resolved through config,
    or a literal model id passed through verbatim. ``json_mode`` sets
    ``response_format={"type": "json_object"}``.
    """
    if not messages:
        raise ValueError("chat() requires at least one message")

    key = _api_key()
    url = f"{base_url()}/chat/completions"
    request = ChatRequest(
        model=_resolve_model(model),
        messages=[m if isinstance(m, ChatMessage) else ChatMessage(**m) for m in messages],
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"} if json_mode else None,
    )
    body = json.dumps(request.to_payload()).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {key}",
    }

    last_error: Exception | None = None
    for attempt in range(_ATTEMPTS):
        if attempt:
            _sleep(_RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)])
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with _open(req, timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = _safe_detail(exc)
            if _is_retryable_status(exc.code):
                last_error = RuntimeHTTPError(exc.code, detail)
                continue
            raise RuntimeHTTPError(exc.code, detail) from exc
        except (socket.timeout, TimeoutError) as exc:
            last_error = exc
            continue
        except urllib.error.URLError as exc:
            # Connection refused / DNS / TLS: a transport failure, retryable.
            last_error = exc
            continue

        try:
            parsed = ChatResponse.model_validate_json(raw)
        except ValidationError as exc:
            raise RuntimeError(
                f"LLM gateway returned an unparseable response: {exc}"
            ) from exc
        return parsed.text()

    if isinstance(last_error, RuntimeHTTPError):
        raise last_error
    raise RuntimeError(
        f"LLM gateway unreachable after {_ATTEMPTS} attempts: {last_error}"
    ) from last_error


def complete(
    prompt: str,
    *,
    system: str | None = None,
    model: str = "cheap",
    temperature: float = 0.7,
    max_tokens: int | None = None,
    json_mode: bool = False,
    timeout: float = 120.0,
) -> str:
    """Convenience wrapper: one optional system message plus one user prompt."""
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return chat(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
        timeout=timeout,
    )
