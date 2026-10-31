"""Tests for Slice 1 (`decision-memory-type`):
- AC-1: Decision model config (`llm_model_decision`) and runtime settings (`settings_store`, `runtime._resolve_model`).
- AC-2: Native `/systemone` decision client (`runtime.decision_url`, Pydantic wire models, `runtime.classify_memory_type` fail-open without retry).
- AC-3: `mind.remember()` classifies default/untyped memories via `clef-flash` with regex fallback and preserves explicit types.
- AC-4: `mind._promote_daily_notes()` and `mind._promotion_kind()` use `clef-flash` with caching (`_CLASSIFY_CACHE`) and regex fallback.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from neobrain import config, db, mind, runtime, settings_store
from neobrain.api import app


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


def http_error(url: str, code: int, body: bytes = b'{"error":"boom"}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(body))


@pytest.fixture(autouse=True)
def _hermetic_llm(monkeypatch):
    """Pin predictable LLM settings and clear classify cache between tests."""
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("NEOBRAIN_LLM_MODEL_DECISION", raising=False)
    monkeypatch.setattr(config.settings, "llm_api_key", "test-key", raising=False)
    monkeypatch.setattr(config.settings, "llm_base_url", "http://127.0.0.1:4000/v1", raising=False)
    if "llm_model_decision" in config.Settings.model_fields:
        monkeypatch.setattr(config.settings, "llm_model_decision", "cloudflare/clef-flash", raising=False)
    if hasattr(mind, "clear_classify_cache"):
        mind.clear_classify_cache()
    yield
    if hasattr(mind, "clear_classify_cache"):
        mind.clear_classify_cache()


# ---------------------------------------------------------------------------
# AC-1: Decision Model Config & Runtime Settings
# ---------------------------------------------------------------------------


def test_ac1_decision_model_config_and_settings(tmp_path, monkeypatch):
    # 1. config.Settings defines llm_model_decision = "cloudflare/clef-flash"
    fresh = config.Settings()
    assert hasattr(fresh, "llm_model_decision")
    assert fresh.llm_model_decision == "cloudflare/clef-flash"

    monkeypatch.setenv("NEOBRAIN_LLM_MODEL_DECISION", "custom/decision-v1")
    assert config.Settings().llm_model_decision == "custom/decision-v1"
    monkeypatch.delenv("NEOBRAIN_LLM_MODEL_DECISION", raising=False)

    # 2. settings_store.REGISTRY includes llm_model_decision under cognition
    by_key = {spec.key: spec for spec in settings_store.REGISTRY}
    assert "llm_model_decision" in by_key
    spec = by_key["llm_model_decision"]
    assert spec.label == "Decision model"
    assert spec.type == "str"
    assert spec.section == "cognition"

    # 3. runtime._resolve_model("decision") resolves to config.settings.llm_model_decision
    monkeypatch.setattr(config.settings, "llm_model_decision", "cloudflare/clef-flash", raising=False)
    assert runtime._resolve_model("decision") == "cloudflare/clef-flash"

    # 4. Runtime update via PUT /api/settings and visibility in settings_store
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    monkeypatch.setattr(config.settings, "life_enabled", "0")
    before = {k: getattr(config.settings, k) for k in settings_store._BY_KEY if hasattr(config.settings, k)}
    try:
        with TestClient(app) as client:
            r = client.put("/api/settings", json={"values": {"llm_model_decision": "cloudflare/clef-pro"}})
            assert r.status_code == 200
            assert "llm_model_decision" in r.json()["applied"]
            assert config.settings.llm_model_decision == "cloudflare/clef-pro"
            assert runtime._resolve_model("decision") == "cloudflare/clef-pro"

            snap = client.get("/api/settings").json()
            cognition_fields = {
                f["key"]: f
                for sec in snap["sections"]
                if sec["id"] == "cognition"
                for f in sec["fields"]
            }
            assert "llm_model_decision" in cognition_fields
            assert cognition_fields["llm_model_decision"]["value"] == "cloudflare/clef-pro"

        conn = db.connect(tmp_path / "neobrain.db")
        try:
            if hasattr(settings_store, "get_public"):
                pub = settings_store.get_public(conn)
                assert "llm_model_decision" in str(pub)
        finally:
            conn.close()
    finally:
        for k, v in before.items():
            setattr(config.settings, k, v)
        config._recompute_derived()


# ---------------------------------------------------------------------------
# AC-2: Native /systemone Decision Client
# ---------------------------------------------------------------------------


def test_ac2_decision_url_and_pydantic_wire_models(monkeypatch):
    monkeypatch.setattr(config.settings, "llm_base_url", "http://127.0.0.1:4000/v1", raising=False)
    assert runtime.decision_url() == "http://127.0.0.1:4000/systemone"

    monkeypatch.setattr(config.settings, "llm_base_url", "http://127.0.0.1:4000/v1/", raising=False)
    assert runtime.decision_url() == "http://127.0.0.1:4000/systemone"

    monkeypatch.setattr(config.settings, "llm_base_url", "http://127.0.0.1:4000", raising=False)
    assert runtime.decision_url() == "http://127.0.0.1:4000/systemone"

    for model_cls_name in ("DecisionQuestion", "DecisionRequest", "DecisionAnswer", "DecisionResponse"):
        cls = getattr(runtime, model_cls_name)
        assert isinstance(cls, type) and issubclass(cls, BaseModel)


def test_ac2_classify_memory_type_request_and_response_variants(monkeypatch):
    captured: list[dict] = []

    def fake_open(req, timeout):
        captured.append(
            {
                "url": req.full_url,
                "method": req.get_method(),
                "auth": req.headers.get("Authorization"),
                "content_type": req.headers.get("Content-type") or req.headers.get("Content-Type"),
                "payload": json.loads(req.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return FakeResponse(
            {
                "model": "cloudflare/clef-flash",
                "answers": {
                    "memory_type": {
                        "choice": "lesson",
                        "probabilities": {
                            "lesson": 0.91,
                            "decision": 0.05,
                            "preference": 0.02,
                            "observation": 0.02,
                        },
                    }
                },
            }
        )

    monkeypatch.setattr(runtime, "_open", fake_open)
    verdict = runtime.classify_memory_type("  Fixed DNS race by acquiring the lock first.  ")
    assert verdict == "lesson"
    assert len(captured) == 1
    call = captured[0]
    assert call["url"] == "http://127.0.0.1:4000/systemone"
    assert call["method"] == "POST"
    assert call["auth"] == "Bearer test-key"
    assert call["timeout"] == 2.0

    payload = call["payload"]
    assert payload["model"] == "cloudflare/clef-flash"
    assert payload["state"] == "Fixed DNS race by acquiring the lock first."
    assert "memory_type" in payload["questions"]
    q = payload["questions"]["memory_type"]
    assert q["type"] == "choice"
    assert isinstance(q["instructions"], str) and len(q["instructions"]) > 10
    assert set(q["criteria"].keys()) == {"lesson", "decision", "preference", "observation"}

    # Response using `value` field when `choice` is absent
    monkeypatch.setattr(
        runtime,
        "_open",
        lambda req, timeout: FakeResponse(
            {"answers": {"memory_type": {"value": "decision"}}}
        ),
    )
    assert runtime.classify_memory_type("We switched the primary cache to SQLite WAL.") == "decision"

    # Response falling back to highest probability in `probabilities` when `choice` and `value` are absent
    monkeypatch.setattr(
        runtime,
        "_open",
        lambda req, timeout: FakeResponse(
            {
                "answers": {
                    "memory_type": {
                        "probabilities": {
                            "observation": 0.10,
                            "preference": 0.82,
                            "decision": 0.05,
                            "lesson": 0.03,
                        }
                    }
                }
            }
        ),
    )
    assert runtime.classify_memory_type("Always format replies as concise bullet lists.") == "preference"


def test_ac2_classify_memory_type_fail_open_without_sleep_or_retry(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(runtime, "_sleep", lambda s: sleeps.append(s))

    # 1. HTTP 500 -> single attempt, returns None, zero sleeps
    calls: list[int] = []

    def fail_500(req, timeout):
        calls.append(1)
        raise http_error(req.full_url, 500)

    monkeypatch.setattr(runtime, "_open", fail_500)
    assert runtime.classify_memory_type("Some memory text") is None
    assert len(calls) == 1
    assert sleeps == []

    # 2. URLError -> single attempt, returns None, zero sleeps
    calls.clear()

    def fail_url(req, timeout):
        calls.append(1)
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(runtime, "_open", fail_url)
    assert runtime.classify_memory_type("Some memory text") is None
    assert len(calls) == 1
    assert sleeps == []

    # 3. TimeoutError / OSError -> single attempt, returns None, zero sleeps
    calls.clear()

    def fail_timeout(req, timeout):
        calls.append(1)
        raise TimeoutError("timed out")

    monkeypatch.setattr(runtime, "_open", fail_timeout)
    assert runtime.classify_memory_type("Some memory text") is None
    assert len(calls) == 1
    assert sleeps == []

    # 4. Malformed JSON -> returns None
    monkeypatch.setattr(runtime, "_open", lambda req, timeout: FakeResponse(b"not-valid-json"))
    assert runtime.classify_memory_type("Some memory text") is None

    # 5. Invalid category -> returns None
    monkeypatch.setattr(
        runtime,
        "_open",
        lambda req, timeout: FakeResponse({"answers": {"memory_type": {"choice": "not_a_type"}}}),
    )
    assert runtime.classify_memory_type("Some memory text") is None

    # 6. Blank text, missing API key, or empty model -> returns None without calling _open
    calls.clear()
    monkeypatch.setattr(runtime, "_open", lambda req, timeout: calls.append(1) or FakeResponse({}))
    assert runtime.classify_memory_type("   ") is None
    assert calls == []

    monkeypatch.setattr(config.settings, "llm_api_key", "", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    assert runtime.classify_memory_type("Valid text with no key") is None
    assert calls == []

    monkeypatch.setattr(config.settings, "llm_api_key", "test-key", raising=False)
    monkeypatch.setattr(config.settings, "llm_model_decision", "", raising=False)
    assert runtime.classify_memory_type("Valid text with empty model") is None
    assert calls == []
    assert sleeps == []


# ---------------------------------------------------------------------------
# AC-3: mind.remember() Classifies Default/Untyped Memories via clef-flash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("predicted", "expected_weight"),
    [
        ("lesson", 0.85),
        ("decision", 0.88),
        ("preference", 0.90),
        ("observation", 0.62),
    ],
)
def test_ac3_remember_classifies_default_and_assigns_weight(
    conn, monkeypatch, predicted: str, expected_weight: float
):
    seen_texts: list[str] = []

    def fake_classify(text: str, **kwargs) -> str | None:
        seen_texts.append(text)
        return predicted

    monkeypatch.setattr(runtime, "classify_memory_type", fake_classify, raising=False)

    atom = mind.remember(
        conn,
        f"Memory statement for {predicted} classification check",
        label="Context label",
    )
    assert atom["type"] == predicted
    row = conn.execute("SELECT type, weight FROM m_atoms WHERE id=?", (atom["id"],)).fetchone()
    assert row["type"] == predicted
    assert row["weight"] == pytest.approx(expected_weight)
    assert seen_texts == [f"Context label: Memory statement for {predicted} classification check"]


def test_ac3_remember_api_classifies_omitted_and_observation_type(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    monkeypatch.setattr(config.settings, "life_enabled", "0")

    verdicts = iter(["lesson", "preference"])
    monkeypatch.setattr(
        runtime, "classify_memory_type", lambda text, **kw: next(verdicts), raising=False
    )

    with TestClient(app) as client:
        r1 = client.post(
            "/api/mind/remember",
            json={"text": "Acquiring the lock before spawning workers prevents the startup race"},
        )
        assert r1.status_code == 200
        assert r1.json()["type"] == "lesson"

        r2 = client.post(
            "/api/mind/remember",
            json={"text": "Always keep responses concise without filler", "type": "observation"},
        )
        assert r2.status_code == 200
        assert r2.json()["type"] == "preference"


@pytest.mark.parametrize(
    "explicit_type",
    ["lesson", "decision", "preference", "open", "dream", "identity"],
)
def test_ac3_remember_preserves_explicit_type_without_calling_classifier(
    conn, monkeypatch, explicit_type: str
):
    assert hasattr(runtime, "classify_memory_type")
    assert hasattr(mind, "_classify_memory_type")

    def forbid_classify(*args, **kwargs):
        raise AssertionError("runtime.classify_memory_type must not be called for explicit non-observation atype")

    monkeypatch.setattr(runtime, "classify_memory_type", forbid_classify)

    atom = mind.remember(
        conn,
        f"Explicitly typed {explicit_type} memory entry",
        atype=explicit_type,
    )
    assert atom["type"] == explicit_type
    row = conn.execute("SELECT type FROM m_atoms WHERE id=?", (atom["id"],)).fetchone()
    assert row["type"] == explicit_type


def test_ac3_remember_falls_back_to_regex_then_observation_when_gateway_returns_none(
    conn, monkeypatch
):
    assert hasattr(mind, "_regex_promotion_kind")
    calls: list[str] = []

    def fake_none(text: str, **kw) -> None:
        calls.append(text)
        return None

    monkeypatch.setattr(runtime, "classify_memory_type", fake_none, raising=False)

    # Lesson via regex fallback
    a_lesson = mind.remember(
        conn,
        "The worker deadlocked because the connection pool was shared across forks.",
        label="Root cause: shared pool across forks",
    )
    assert a_lesson["type"] == "lesson"

    # Decision via regex fallback
    a_decision = mind.remember(
        conn,
        "We decided to pin the model router on all four subagents.",
        label="Decision: pin subagent router",
    )
    assert a_decision["type"] == "decision"

    # Neutral statement defaults to observation
    a_obs = mind.remember(
        conn,
        "Checked the local service status output during afternoon inspection.",
    )
    assert a_obs["type"] == "observation"
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# AC-4: mind._promote_daily_notes() & mind._promotion_kind() with Caching
# ---------------------------------------------------------------------------


def _insert_daily_note_atom(
    conn,
    atom_id: str,
    *,
    label: str,
    text: str,
    atype: str = "observation",
    source: str = "memory/2026-10-29.md",
    created: int = 1_760_000_000,
    hub: str = "agent",
) -> None:
    conn.execute(
        "INSERT INTO m_atoms(id,label,type,created,text,source,tags,weight,hub,hash) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (atom_id, label, atype, created, text, source, hub, 0.5, hub, f"h-{atom_id}"),
    )
    conn.commit()


def test_ac4_promote_daily_notes_promotes_semantic_lesson_and_decision_without_regex_keywords(
    conn, monkeypatch
):
    # Daily notes with zero regex trigger words ("fixed", "root cause", "decided", etc.)
    _insert_daily_note_atom(
        conn,
        "a_note_lesson",
        label="Connection pool behaviour under fork",
        text=(
            "Sharing a single SQLite file handle across forked worker processes "
            "corrupts the WAL index header under concurrent writes; each worker "
            "must open its own connection after fork."
        ),
    )
    _insert_daily_note_atom(
        conn,
        "a_note_decision",
        label="Memory classification architecture",
        text=(
            "All untyped memory writes and daily note sections now route through "
            "the System One endpoint on port 4000 with a two-second timeout."
        ),
        created=1_760_000_001,
    )

    def fake_classify(text: str, **kwargs) -> str | None:
        if "SQLite file handle" in text:
            return "lesson"
        if "System One endpoint" in text:
            return "decision"
        return "observation"

    monkeypatch.setattr(runtime, "classify_memory_type", fake_classify, raising=False)

    res = mind._promote_daily_notes(conn, embed=False)
    assert len(res["promoted"]) == 2

    promoted_rows = {
        r["label"]: r
        for r in conn.execute("SELECT id, label, type, weight FROM m_atoms WHERE id GLOB 'm_pd_*'")
    }
    assert promoted_rows["Connection pool behaviour under fork"]["type"] == "lesson"
    assert promoted_rows["Memory classification architecture"]["type"] == "decision"

    # Verify derived-from edges exist from promoted atoms to original doc atoms
    edges = {
        (r["source"], r["target"], r["type"])
        for r in conn.execute("SELECT source, target, type FROM m_edges WHERE type='derived-from'")
    }
    assert (
        promoted_rows["Connection pool behaviour under fork"]["id"],
        "a_note_lesson",
        "derived-from",
    ) in edges
    assert (
        promoted_rows["Memory classification architecture"]["id"],
        "a_note_decision",
        "derived-from",
    ) in edges


def test_ac4_promote_daily_notes_suppresses_keyword_false_positives_when_classified_observation(
    conn, monkeypatch
):
    # Contains regex keywords ("built", "switched", "shipped") in a routine status log,
    # but clef-flash classifies it as "observation" -> must NOT be promoted.
    _insert_daily_note_atom(
        conn,
        "a_status_log",
        label="Afternoon status update — built and switched containers",
        text=(
            "We built the container image, switched the staging tab in the browser, "
            "and shipped a quick ping to check the logs while watching metrics."
        ),
    )

    monkeypatch.setattr(
        runtime, "classify_memory_type", lambda text, **kw: "observation", raising=False
    )

    res = mind._promote_daily_notes(conn, embed=False)
    assert res["promoted"] == []
    count = conn.execute("SELECT COUNT(*) FROM m_atoms WHERE id GLOB 'm_pd_*'").fetchone()[0]
    assert count == 0


def test_ac4_promote_daily_notes_falls_back_to_regex_when_gateway_unreachable(
    conn, monkeypatch
):
    assert hasattr(mind, "_regex_promotion_kind")
    _insert_daily_note_atom(
        conn,
        "a_regex_lesson",
        label="Root cause: stale PID file after crash",
        text=(
            "The daemon refused to restart because the stale PID file remained on "
            "tmpfs after the power loss; removing it in ExecStartPre solved it."
        ),
    )

    calls: list[str] = []

    def fake_none(text: str, **kw) -> None:
        calls.append(text)
        return None

    monkeypatch.setattr(runtime, "classify_memory_type", fake_none, raising=False)

    res = mind._promote_daily_notes(conn, embed=False)
    assert len(calls) == 1
    assert len(res["promoted"]) == 1
    row = conn.execute("SELECT type FROM m_atoms WHERE id=?", (res["promoted"][0],)).fetchone()
    assert row["type"] == "lesson"


def test_ac4_promote_daily_notes_skips_short_and_already_curated_and_caches_verdicts(
    conn, monkeypatch
):
    # 1. Short section (< 40 chars) and already-curated section must not call classifier
    _insert_daily_note_atom(
        conn,
        "a_short",
        label="Quick note",
        text="Too short to promote.",
    )
    _insert_daily_note_atom(
        conn,
        "a_already_curated",
        label="Already a lesson",
        text="This section was already typed as a lesson during markdown parse and exceeds forty chars.",
        atype="lesson",
    )
    _insert_daily_note_atom(
        conn,
        "a_denied_heading",
        label="Open items: pending investigation",
        text="This heading matches _PROMOTE_DENY so it must be rejected before calling the classifier.",
    )
    _insert_daily_note_atom(
        conn,
        "a_real_candidate",
        label="Gateway timeout tuning",
        text=(
            "Setting a strict two-second socket timeout on synchronous classification "
            "prevents a hung upstream from blocking memory writes."
        ),
    )

    open_calls: list[str] = []

    def fake_open_fail_then_succeed(req, timeout):
        payload = json.loads(req.data.decode("utf-8"))
        open_calls.append(payload["state"])
        if len(open_calls) == 1:
            raise urllib.error.URLError("temporary gateway hiccup")
        return FakeResponse({"answers": {"memory_type": {"choice": "decision"}}})

    monkeypatch.setattr(runtime, "_open", fake_open_fail_then_succeed)

    # First pass: gateway fails (returns None) -> not cached, falls back to regex (no match -> 0 promoted)
    res1 = mind._promote_daily_notes(conn, embed=False)
    assert res1["promoted"] == []
    assert len(open_calls) == 1  # only a_real_candidate was sent to _open

    # Second pass: since None was NOT cached, a_real_candidate retries and succeeds -> cached!
    res2 = mind._promote_daily_notes(conn, embed=False)
    assert len(res2["promoted"]) == 1
    assert len(open_calls) == 2

    # Third pass over unchanged notes: verdict is cached in _CLASSIFY_CACHE -> _open NOT called again
    res3 = mind._promote_daily_notes(conn, embed=False)
    assert res3["promoted"] == []
    assert len(open_calls) == 2

    # Clearing cache forces a fresh query on the next pass
    mind.clear_classify_cache()
    mind._promote_daily_notes(conn, embed=False)
    assert len(open_calls) == 3
