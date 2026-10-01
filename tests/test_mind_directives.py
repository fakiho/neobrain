"""Standing directives: the `[pin]` marker and the pinned-preference lane.

The lane backs the OpenCode adapter's per-call re-injection of must-follow
rules. These tests cover the marker parsing, the deterministic reader, and the
pin/unpin round trip (mind + API).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from neobrain import config, mind
from neobrain.api import app


def test_pin_tags_marks_and_strips_marker():
    tags, text = mind._pin_tags(["agent"], "Prefer short answers. [pin]")
    assert "pin" in tags
    assert text == "Prefer short answers."


def test_pin_tags_noop_without_marker():
    tags, text = mind._pin_tags(["agent"], "Prefer short answers.")
    assert tags == ["agent"]
    assert text == "Prefer short answers."


def test_identity_parser_captures_pin_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    doc = tmp_path / "USER.md"
    doc.write_text("## Directives\n- Prefer bullets over tables. [pin]\n- Avoid jargon whenever you reply.\n")
    atoms, edges, hubs_seen = {}, set(), set()
    mind._parse_identity(doc, atoms, edges, hubs_seen)

    prefs = [a for a in atoms.values() if a["type"] == "preference"]
    assert len(prefs) == 2
    pinned = [a for a in prefs if "pin" in a["tags"]]
    assert len(pinned) == 1
    assert "[pin]" not in pinned[0]["text"]
    assert pinned[0]["text"].startswith("Prefer bullets")


def test_directives_falls_back_then_honors_pin(conn):
    a = mind.remember(conn, "Prefer bullet lists over tables.", atype="preference")
    b = mind.remember(conn, "Always answer in the user's language.", atype="preference")

    fallback = mind.directives(conn, limit=8)
    assert fallback["pinned"] is False
    ids = {x["id"] for x in fallback["atoms"]}
    assert {a["id"], b["id"]} <= ids

    mind.set_pin(conn, b["id"], True)
    pinned = mind.directives(conn, limit=8)
    assert pinned["pinned"] is True
    assert [x["id"] for x in pinned["atoms"]] == [b["id"]]

    # Pinned atoms sort ahead of the fallback pool, capped by limit.
    assert mind.directives(conn, limit=1)["atoms"][0]["id"] == b["id"]

    mind.set_pin(conn, b["id"], False)
    assert mind.directives(conn)["pinned"] is False


def test_set_pin_unknown_atom_raises(conn):
    import pytest

    with pytest.raises(ValueError):
        mind.set_pin(conn, "m_doesnotexist", True)


def test_directives_and_pin_api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    monkeypatch.setattr(config.settings, "life_enabled", "0")
    with TestClient(app) as client:
        r = client.post(
            "/api/mind/remember",
            json={"text": "Prefer short, direct answers.", "type": "preference"},
        )
        atom = r.json()

        body = client.get("/api/mind/directives").json()
        assert body["pinned"] is False
        assert any(x["id"] == atom["id"] for x in body["atoms"])

        pin = client.post("/api/mind/pin", json={"atom_id": atom["id"]}).json()
        assert pin["pinned"] is True and pin["changed"] is True

        body = client.get("/api/mind/directives").json()
        assert body["pinned"] is True
        assert [x["id"] for x in body["atoms"]] == [atom["id"]]

        off = client.post(
            "/api/mind/pin", json={"atom_id": atom["id"], "pinned": False}
        ).json()
        assert off["pinned"] is False and off["changed"] is True

        missing = client.post("/api/mind/pin", json={"atom_id": "m_nope"})
        assert missing.status_code == 400
