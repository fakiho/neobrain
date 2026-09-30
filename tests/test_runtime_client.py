"""Tests for neobrain.runtime — no network, fake gateway via _open/_sleep."""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from neobrain import config, runtime


class FakeResponse:
    def __init__(self, body, status: int = 200):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def ok_body(content: str = "alive") -> dict:
    return {
        "id": "chatcmpl-1",
        "model": "deepseek/deepseek-v4-flash",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content},
             "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    }


def http_error(url: str, code: int, body: bytes = b'{"error":"boom"}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(body))


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    """A predictable key and base URL; no LITELLM_API_KEY leaking in from env."""
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.setattr(config.settings, "llm_api_key", "test-key", raising=False)
    monkeypatch.setattr(config.settings, "llm_base_url", "http://gw.test/v1", raising=False)
    monkeypatch.setattr(config.settings, "llm_model_cheap", "cheap-model", raising=False)
    monkeypatch.setattr(config.settings, "llm_model_strong", "strong-model", raising=False)


def test_cheap_tier_resolves_and_request_is_well_formed(monkeypatch):
    seen = {}

    def fake_open(req, timeout):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        seen["payload"] = json.loads(req.data)
        seen["timeout"] = timeout
        return FakeResponse(ok_body())

    monkeypatch.setattr(runtime, "_open", fake_open)
    out = runtime.chat([{"role": "user", "content": "hi"}], model="cheap", max_tokens=7)

    assert out == "alive"
    assert seen["url"] == "http://gw.test/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    assert seen["payload"]["model"] == "cheap-model"
    assert seen["payload"]["max_tokens"] == 7
    assert seen["payload"]["temperature"] == 0.7
    assert seen["payload"]["messages"] == [{"role": "user", "content": "hi"}]
    assert "response_format" not in seen["payload"]


def test_strong_tier_and_literal_model(monkeypatch):
    seen = []
    monkeypatch.setattr(runtime, "_open", lambda req, timeout: (
        seen.append(json.loads(req.data)["model"]) or FakeResponse(ok_body())
    ))
    runtime.chat([{"role": "user", "content": "x"}], model="strong")
    runtime.chat([{"role": "user", "content": "x"}], model="vendor/some-model")
    assert seen == ["strong-model", "vendor/some-model"]


def test_json_mode_sets_response_format(monkeypatch):
    seen = {}

    def fake_open(req, timeout):
        seen["payload"] = json.loads(req.data)
        return FakeResponse(ok_body("{}"))

    monkeypatch.setattr(runtime, "_open", fake_open)
    runtime.chat([{"role": "user", "content": "x"}], json_mode=True)
    assert seen["payload"]["response_format"] == {"type": "json_object"}


def test_missing_key_raises_config_error(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_api_key", "", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    with pytest.raises(runtime.RuntimeConfigError, match="API key"):
        runtime.chat([{"role": "user", "content": "x"}])


def test_key_from_env_when_config_empty(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_api_key", "", raising=False)
    monkeypatch.setenv("LITELLM_API_KEY", "env-key")
    seen = {}
    monkeypatch.setattr(runtime, "_open", lambda req, timeout: (
        seen.update(auth=req.headers.get("Authorization")) or FakeResponse(ok_body())
    ))
    runtime.chat([{"role": "user", "content": "x"}])
    assert seen["auth"] == "Bearer env-key"


def test_retries_on_500_then_succeeds(monkeypatch):
    calls = []
    sleeps = []

    def fake_open(req, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise http_error(req.full_url, 500)
        return FakeResponse(ok_body("recovered"))

    monkeypatch.setattr(runtime, "_open", fake_open)
    monkeypatch.setattr(runtime, "_sleep", lambda s: sleeps.append(s))
    assert runtime.chat([{"role": "user", "content": "x"}]) == "recovered"
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_retries_on_429_then_succeeds(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, "_sleep", lambda s: None)

    def fake_open(req, timeout):
        calls.append(1)
        if len(calls) < 3:
            raise http_error(req.full_url, 429)
        return FakeResponse(ok_body("ok"))

    monkeypatch.setattr(runtime, "_open", fake_open)
    assert runtime.chat([{"role": "user", "content": "x"}]) == "ok"
    assert len(calls) == 3


def test_4xx_fails_fast_without_retry(monkeypatch):
    calls = []
    sleeps = []
    monkeypatch.setattr(runtime, "_sleep", lambda s: sleeps.append(s))

    def fake_open(req, timeout):
        calls.append(1)
        raise http_error(req.full_url, 400, b'{"error":{"message":"bad model"}}')

    monkeypatch.setattr(runtime, "_open", fake_open)
    with pytest.raises(runtime.RuntimeHTTPError) as err:
        runtime.chat([{"role": "user", "content": "x"}])
    assert err.value.status == 400
    assert len(calls) == 1
    assert sleeps == []
    assert "bad model" in str(err.value)


def test_retries_exhausted_raises_http_error(monkeypatch):
    calls = []
    sleeps = []
    monkeypatch.setattr(runtime, "_sleep", lambda s: sleeps.append(s))

    def fake_open(req, timeout):
        calls.append(1)
        raise http_error(req.full_url, 503)

    monkeypatch.setattr(runtime, "_open", fake_open)
    with pytest.raises(runtime.RuntimeHTTPError) as err:
        runtime.chat([{"role": "user", "content": "x"}])
    assert err.value.status == 503
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


def test_connection_error_retried_then_raises_runtime_error(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, "_sleep", lambda s: None)

    def fake_open(req, timeout):
        calls.append(1)
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(runtime, "_open", fake_open)
    with pytest.raises(RuntimeError, match="unreachable"):
        runtime.chat([{"role": "user", "content": "x"}])
    assert len(calls) == 3


def test_socket_timeout_is_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, "_sleep", lambda s: None)

    def fake_open(req, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("timed out")
        return FakeResponse(ok_body("late"))

    monkeypatch.setattr(runtime, "_open", fake_open)
    assert runtime.chat([{"role": "user", "content": "x"}]) == "late"
    assert len(calls) == 2


def test_unparseable_response_raises_runtime_error(monkeypatch):
    monkeypatch.setattr(runtime, "_open", lambda req, timeout: FakeResponse(b"not json"))
    with pytest.raises(RuntimeError, match="unparseable"):
        runtime.chat([{"role": "user", "content": "x"}])


def test_empty_choices_raises(monkeypatch):
    monkeypatch.setattr(
        runtime, "_open",
        lambda req, timeout: FakeResponse({"id": "x", "choices": []}),
    )
    with pytest.raises(RuntimeError, match="no choices"):
        runtime.chat([{"role": "user", "content": "x"}])


def test_empty_content_raises_with_reasoning_hint(monkeypatch):
    body = {
        "id": "x",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": ""},
             "finish_reason": "length"}
        ],
    }
    monkeypatch.setattr(runtime, "_open", lambda req, timeout: FakeResponse(body))
    with pytest.raises(RuntimeError, match="empty content.*length"):
        runtime.chat([{"role": "user", "content": "x"}])


def test_empty_messages_rejected():
    with pytest.raises(ValueError):
        runtime.chat([])


def test_complete_builds_system_and_user_messages(monkeypatch):
    seen = {}

    def fake_open(req, timeout):
        seen["payload"] = json.loads(req.data)
        return FakeResponse(ok_body("done"))

    monkeypatch.setattr(runtime, "_open", fake_open)
    out = runtime.complete("say hi", system="be brief", model="strong", max_tokens=3)
    assert out == "done"
    assert seen["payload"]["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "say hi"},
    ]
    assert seen["payload"]["model"] == "strong-model"
