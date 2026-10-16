"""Core memory-write quality gate: filler is refused, real memories are kept.

Hermetic: a temp store, no network. The gate lives in ``mind.remember`` so the
HTTP API, CLI and library all share one policy; nothing here depends on a
client, OS, host, or personal name.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from neobrain import cli, config, db, mind
from neobrain.api import app


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    monkeypatch.setattr(config.settings, "remember_quality", "1")
    monkeypatch.setattr(config.settings, "remember_min_chars", 3)
    c = db.connect(tmp_path / "neobrain.db")
    db.init_db(c)
    yield c
    c.close()


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM m_atoms").fetchone()[0]


FILLER = [
    "ok", "OK.", "okay!", "thanks", "Thank you", "thanks so much!", "done",
    "got it", "Sounds good", "lgtm", "ok thanks", "ok, thank you very much",
    "Done.", "continue", "...", "---", "👍", "n/a", "I see", "no problem",
    "it is done now", "all good, thanks",
]

KEEP = [
    "Use tabs",
    "API key rotated",
    "Prefer bullet lists over tables.",
    "ok to ship the fix once CI passes",
    "Fixed the race in the retry loop by taking the lock first",
    "Port 9192 is the daemon default",
    "Deploys go out on Tuesdays",
    "no force pushes to main",
    "Done criteria: all tests green and docs updated",
    "Always answer in the user's language.",
    "Zoom link: https://example.com/room/42",
]


@pytest.mark.parametrize("text", FILLER)
def test_filler_is_rejected_without_writing(conn, text):
    with pytest.raises(mind.RememberRejected) as exc:
        mind.remember(conn, text)
    assert exc.value.code == "filler"
    assert exc.value.reason
    assert _count(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM m_ops WHERE op='store'").fetchone()[0] == 0


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_empty_is_rejected_with_empty_code(conn, text):
    with pytest.raises(mind.RememberRejected) as exc:
        mind.remember(conn, text)
    assert exc.value.code == "empty"
    assert _count(conn) == 0


@pytest.mark.parametrize("text", KEEP)
def test_legitimate_short_memories_are_kept(conn, text):
    atom = mind.remember(conn, text)
    assert atom["id"].startswith("m_")
    assert _count(conn) == 1


def test_rejection_is_a_value_error_and_has_portable_dict(conn):
    with pytest.raises(ValueError) as exc:
        mind.remember(conn, "thanks")
    assert exc.value.as_dict() == {
        "stored": False, "rejected": True, "code": "filler", "reason": exc.value.reason,
    }


def test_quality_gate_can_be_disabled(conn, monkeypatch):
    monkeypatch.setattr(config.settings, "remember_quality", "0")
    assert mind.remember(conn, "thanks")["id"].startswith("m_")
    # empty text is never storable, even with the gate off
    with pytest.raises(mind.RememberRejected) as exc:
        mind.remember(conn, "  ")
    assert exc.value.code == "empty"


def test_min_chars_is_configurable(conn, monkeypatch):
    monkeypatch.setattr(config.settings, "remember_min_chars", 12)
    with pytest.raises(mind.RememberRejected):
        mind.remember(conn, "Use tabs")
    monkeypatch.setattr(config.settings, "remember_min_chars", 3)
    assert mind.remember(conn, "Use tabs")["id"].startswith("m_")


def test_dedupe_still_idempotent_for_real_memory(conn):
    a = mind.remember(conn, "Deploys go out on Tuesdays", source="s", dedupe=True)
    b = mind.remember(conn, "Deploys go out on Tuesdays", source="s", dedupe=True)
    assert b["id"] == a["id"] and b["deduped"] is True
    assert _count(conn) == 1


# --- HTTP API (any client) ---------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    monkeypatch.setattr(config.settings, "life_enabled", "0")
    monkeypatch.setattr(config.settings, "remember_quality", "1")
    monkeypatch.setattr(config.settings, "remember_min_chars", 3)
    with TestClient(app) as c:
        yield c


def test_api_rejects_filler_with_422_and_stable_body(client):
    r = client.post("/api/mind/remember", json={"text": "thanks!"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["stored"] is False and detail["rejected"] is True
    assert detail["code"] == "filler" and detail["reason"]
    assert client.get("/api/mind/memories").json()["total"] == 0


def test_api_rejects_empty_with_empty_code(client):
    r = client.post("/api/mind/remember", json={"text": "   "})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "empty"


def test_api_stores_short_real_memory_with_any_session(client):
    r = client.post(
        "/api/mind/remember",
        json={"text": "Use tabs", "type": "preference", "session": "any-client-id"},
    )
    assert r.status_code == 200
    assert r.json()["id"].startswith("m_")
    assert client.get("/api/mind/memories").json()["total"] == 1


def test_api_without_session_field_works_for_non_opencode_clients(client):
    r = client.post("/api/mind/remember", json={"text": "Deploys go out on Tuesdays"})
    assert r.status_code == 200


# --- CLI ----------------------------------------------------------------


def test_cli_remember_rejects_filler_with_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    monkeypatch.setattr(config.settings, "remember_quality", "1")
    monkeypatch.setattr(config.settings, "remember_min_chars", 3)
    code = cli.main(["remember", "thanks"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert out["rejected"] is True and out["code"] == "filler"


def test_cli_remember_stores_real_memory(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    monkeypatch.setattr(config.settings, "remember_quality", "1")
    monkeypatch.setattr(config.settings, "remember_min_chars", 3)
    code = cli.main(["remember", "Use tabs"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["id"].startswith("m_")


SENTENCE_FILLER = [
    "what would you like to do next",
    "What would you like to do next?",
    "the tool outputs show the command succeeded",
    "Let me know if you need anything else",
    "let me know if you need anything else!",
    "Is there anything else you need?",
    "Do you need anything else?",
    "What's next?",
]

NEAR_MISS_REAL = [
    "Let me know in #infra before changing the deploy window",
    "Let me know if the deploy fails, then roll back to v2",
    "The test results show the migration succeeded on staging only",
    "Is there anything else in the cache dir? Only .lock files",
    "What next step: run alembic upgrade head",
]


@pytest.mark.parametrize("text", SENTENCE_FILLER)
def test_sentence_filler_is_rejected(text):
    rej = mind.remember_rejection(text)
    assert rej is not None and rej.code == "filler"


@pytest.mark.parametrize("text", NEAR_MISS_REAL)
def test_sentences_containing_filler_words_are_kept(text):
    assert mind.remember_rejection(text) is None
