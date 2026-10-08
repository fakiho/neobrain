"""API smoke tests: a real FastAPI TestClient over a temp store.

Hermetic: ``config.DB_PATH``/``DATA_DIR`` point into ``tmp_path`` and the life
loop is disabled, so no thread, no network and no LLM are ever touched.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from neobrain import config
from neobrain.api import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    monkeypatch.setattr(config.settings, "life_enabled", "0")
    # TestClient runs the app lifespan on __enter__ (init_db + boot announcement).
    with TestClient(app) as c:
        yield c


def test_health_ok_and_empty_mind_is_announced(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    # SPEC §14 S6: booting a brand-new empty mind must be a visible event.
    events = client.get("/api/events", params={"source": "neobrain"}).json()
    assert events["count"] == 1
    assert "empty mind" in events["events"][0]["title"]


def test_static_dashboard_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_mind_remember_recall_roundtrip(client):
    r = client.post(
        "/api/mind/remember",
        json={"text": "the daemon listened on 127.0.0.1:9477", "hubs": ["neobrain"]},
    )
    assert r.status_code == 200
    atom = r.json()
    assert atom["id"].startswith("m_")

    pack = client.get("/api/mind/recall", params={"q": "daemon port"}).json()
    assert pack["count"] >= 1
    assert any(a["id"] == atom["id"] for a in pack["atoms"])

    feed = client.get("/api/mind/memories", params={"q": "daemon"}).json()
    assert any(i["id"] == atom["id"] for i in feed["items"])

    detail = client.get(f"/api/mind/atom/{atom['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == atom["id"]
    assert client.get("/api/mind/atom/does-not-exist").status_code == 404


def test_feedback_endpoint_records_and_rejects(client):
    atom = client.post("/api/mind/remember", json={"text": "feedback target"}).json()
    ok = client.post("/api/mind/feedback", json={"atom_id": atom["id"], "signal": "useful"})
    assert ok.status_code == 200
    assert ok.json()["recorded"] is True

    bad = client.post("/api/mind/feedback", json={"atom_id": atom["id"], "signal": "bogus"})
    assert bad.status_code == 400


def test_mind_views_smoke(client):
    client.post("/api/mind/remember", json={"text": "graph node one", "hubs": ["agent"]})
    assert client.get("/api/mind/stats").json()["atoms"] >= 1
    assert client.get("/api/mind/graph").json()["nodes"]
    wake = client.get("/api/mind/wakeup").json()
    assert set(wake) == {"identity", "open_loops", "recent", "hubs"}
    assert client.get("/api/mind/dreams").json()["count"] >= 0
    assert client.get("/api/mind/activity").json()["ops"] is not None


def test_life_loop_disabled_runs_no_phase(client):
    # NEOBRAIN_LIFE_ENABLED=0 must leave the loop entirely idle.
    assert client.get("/api/events", params={"source": "life"}).json()["count"] == 0


def test_debug_logs_endpoint_is_on_demand_and_safe(client):
    # Fetched only when the Debug tab asks; best-effort even without journalctl.
    r = client.get("/api/debug/logs", params={"lines": 50})
    assert r.status_code == 200
    body = r.json()
    assert body["unit"] == "neobrain.service"
    assert isinstance(body["lines"], list)
    assert client.get("/api/debug/logs", params={"lines": 99999}).status_code == 422


def test_lifespan_starts_and_stops_loop_when_enabled(tmp_path, monkeypatch):
    """The daemon owns the loop: it starts on boot and is joined+closed on exit.

    A fake loop replaces LifeLoop so this stays hermetic (the real one probes
    the network); it also guards the thread-affine close regression.
    """
    import time

    from neobrain import life

    seen: list[str] = []

    class FakeLoop:
        def __init__(self, *args, **kwargs):
            pass

        def tick(self):
            seen.append("tick")

        def close(self):
            seen.append("close")

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    monkeypatch.setattr(config.settings, "life_enabled", "1")
    monkeypatch.setattr(life, "LifeLoop", FakeLoop)

    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200
        time.sleep(0.1)

    assert "tick" in seen  # the loop ran inside the daemon
    assert seen[-1] == "close"  # and its own handle was closed on shutdown
