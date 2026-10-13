"""Runtime settings: the store, the token gate, and the API endpoints.

Hermetic: a temp store, no network (the one probe test points at a closed
port), and every touched ``config.settings`` field is restored afterwards.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from neobrain import config, db, settings_store
from neobrain.api import app


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A migrated temp store, with settings restored after the test."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    before = {k: getattr(config.settings, k) for k in settings_store._BY_KEY}
    conn = db.connect(tmp_path / "neobrain.db")
    db.init_db(conn)
    yield conn
    conn.close()
    for key, value in before.items():
        setattr(config.settings, key, value)
    config._recompute_derived()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    monkeypatch.setattr(config.settings, "life_enabled", "0")
    before = {k: getattr(config.settings, k) for k in settings_store._BY_KEY}
    with TestClient(app) as c:
        yield c
    for key, value in before.items():
        setattr(config.settings, key, value)
    config._recompute_derived()


def test_registry_covers_real_settings_fields():
    for spec in settings_store.REGISTRY:
        assert spec.key in config.Settings.model_fields, spec.key
        assert spec.section in {s[0] for s in settings_store.SECTIONS}


def test_set_values_persists_applies_and_reverts(store):
    out = settings_store.set_values(store, {"llm_model_cheap": "vendor/small"})
    assert "llm_model_cheap" in out["applied"]
    assert config.settings.llm_model_cheap == "vendor/small"  # applied live

    # stored, so a fresh read sees it
    assert config.db_overrides()["llm_model_cheap"] == "vendor/small"

    # None reverts to the env default
    out = settings_store.set_values(store, {"llm_model_cheap": None})
    assert "llm_model_cheap" in out["reverted"]
    assert config.settings.llm_model_cheap == config.Settings().llm_model_cheap


def test_set_values_rejects_unknown_env_only_and_out_of_range(store):
    with pytest.raises(ValueError):
        settings_store.set_values(store, {"nope": 1})
    with pytest.raises(ValueError):
        settings_store.set_values(store, {"data_dir": "/tmp/x"})  # env-only
    with pytest.raises(ValueError):
        settings_store.set_values(store, {"life_tick_seconds": 1})  # below min


def test_blank_secret_keeps_the_stored_one(store):
    settings_store.set_values(store, {"llm_api_key": "sk-secret"})
    assert config.db_overrides()["llm_api_key"] == "sk-secret"
    settings_store.set_values(store, {"llm_api_key": ""})  # blank = no-op
    assert config.db_overrides()["llm_api_key"] == "sk-secret"


def test_snapshot_never_returns_a_secret(store):
    settings_store.set_values(store, {"llm_api_key": "sk-secret"})
    snap = settings_store.snapshot(store)
    fields = {f["key"]: f for sec in snap["sections"] for f in sec["fields"]}
    key = fields["llm_api_key"]
    assert key["secret"] is True
    assert key["value"] == "" and key["default"] == ""
    assert key["set"] is True
    assert "sk-secret" not in str(snap)


def test_boolean_override_is_coerced_and_gates_the_life_loop(store):
    settings_store.set_values(store, {"dreams_enabled": "0"})
    assert config.settings.dreams_enabled == "0"
    settings_store.set_values(store, {"dreams_enabled": True})
    assert config.settings.dreams_enabled == "1"


def test_token_gate(monkeypatch):
    monkeypatch.setattr(config.settings, "settings_token", "")
    assert settings_store.token_ok("") is True          # unprotected
    monkeypatch.setattr(config.settings, "settings_token", "abc")
    assert settings_store.token_ok("") is False
    assert settings_store.token_ok("abc") is True
    assert settings_store.token_ok("abd") is False


# --- API ---------------------------------------------------------------


def test_get_settings_lists_sections_and_flags(client):
    d = client.get("/api/settings").json()
    assert d["token_required"] is False
    ids = [s["id"] for s in d["sections"]]
    assert "features" in ids and "cognition" in ids
    keys = {f["key"] for s in d["sections"] for f in s["fields"]}
    assert {"dreams_enabled", "llm_model_strong", "settings_token"} <= keys
    assert any(p["id"] == "litellm" for p in d["providers"])


def test_put_settings_applies_and_requires_token(client, monkeypatch):
    # no token configured: the write is allowed
    r = client.put("/api/settings", json={"values": {"act_enabled": "0"}})
    assert r.status_code == 200
    assert "act_enabled" in r.json()["applied"]
    assert config.settings.act_enabled == "0"

    # configure a token: the same write is now refused without it
    monkeypatch.setattr(config.settings, "settings_token", "s3cret")
    assert client.put("/api/settings", json={"values": {"act_enabled": "1"}}).status_code == 401
    ok = client.put(
        "/api/settings",
        json={"values": {"act_enabled": "1"}},
        headers={"X-NeoBrain-Token": "s3cret"},
    )
    assert ok.status_code == 200
    assert config.settings.act_enabled == "1"


def test_put_settings_rejects_bad_key(client):
    r = client.put("/api/settings", json={"values": {"not_a_key": 1}})
    assert r.status_code == 400


def test_models_endpoint_reports_unreachable_provider(client):
    r = client.post("/api/settings/models", json={"base_url": "http://127.0.0.1:9/v1"})
    assert r.status_code == 502


def test_test_endpoint_needs_a_model(client):
    r = client.post("/api/settings/test", json={"base_url": "http://127.0.0.1:9/v1", "model": ""})
    assert r.status_code == 400
