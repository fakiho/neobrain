"""Debug overview link provenance and atom-detail explicit flags.

Hermetic: the ``client`` fixture points the store into ``tmp_path``. These
assert the contract, not the current overview shape — a missing ``links``
object or a bare edge counted as hidden is a failure.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from neobrain import config, db
from neobrain.api import app

_LINK_KEYS = {"total", "explicit", "guessed", "unmarked", "supersedes_explicit", "hidden"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    monkeypatch.setattr(config.settings, "life_enabled", "0")
    with TestClient(app) as c:
        yield c


def _remember(client, text: str, **extra) -> dict:
    r = client.post("/api/mind/remember", json={"text": text, **extra})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"].startswith("m_")
    return body


def _overview(client) -> dict:
    r = client.get("/api/debug/overview")
    assert r.status_code == 200, r.text
    return r.json()


def test_fresh_overview_link_counts_are_zero(client):
    ov = _overview(client)
    assert "links" in ov
    links = ov["links"]
    assert set(links) >= _LINK_KEYS
    for key in _LINK_KEYS:
        assert links[key] == 0


def test_explicit_supersedes_hides_active_target(client):
    source = _remember(client, "gateway now uses tls")
    target = _remember(client, "gateway uses plaintext")
    linked = client.post(
        "/api/mind/link",
        json={"source_id": source["id"], "target_id": target["id"], "kind": "supersedes"},
    )
    assert linked.status_code == 200, linked.text

    links = _overview(client)["links"]
    assert links["explicit"] >= 1
    assert links["supersedes_explicit"] >= 1
    assert links["hidden"] == 1


def test_remember_links_field_is_unmarked_and_does_not_hide(client):
    target = _remember(client, "gateway allows icmp")
    before = _overview(client)["links"]
    _remember(
        client,
        "gateway rate-limits icmp",
        links={"supersedes": [target["id"]]},
    )
    links = _overview(client)["links"]
    # remember() also writes one unmarked `about` edge to the new atom's hub,
    # so the bare supersedes edge is the second new unmarked row.
    assert links["unmarked"] == before["unmarked"] + 2
    assert links["hidden"] == before["hidden"]
    assert links["explicit"] == before["explicit"]
    assert links["supersedes_explicit"] == before["supersedes_explicit"]


def test_link_feed_lists_explicit_supersedes_and_omits_about(client):
    source = _remember(client, "source memory for the feed", label="feed source")
    target = _remember(client, "target memory for the feed", label="feed target")
    linked = client.post(
        "/api/mind/link",
        json={"source_id": source["id"], "target_id": target["id"], "kind": "supersedes"},
    )
    assert linked.status_code == 200, linked.text

    ov = _overview(client)
    assert isinstance(ov["link_feed"], list)
    match = [
        item for item in ov["link_feed"]
        if item.get("type") == "supersedes" and item.get("explicit") is True
    ]
    assert match, ov["link_feed"]
    item = match[0]
    assert item["source_label"]
    assert item["target_label"]
    assert all(row.get("type") != "about" for row in ov["link_feed"])


def test_atom_detail_marks_explicit_and_bare_edges(client):
    stated_target = _remember(client, "old stated fact")
    source = _remember(client, "new stated fact")
    bare_target = _remember(client, "old guessed fact")
    stated = client.post(
        "/api/mind/link",
        json={
            "source_id": source["id"],
            "target_id": stated_target["id"],
            "kind": "supersedes",
            "session": "ses_stated",
        },
    )
    assert stated.status_code == 200, stated.text
    bare = _remember(
        client,
        "new guessed fact",
        links={"supersedes": [bare_target["id"]]},
    )

    explicit_links = client.get(f"/api/mind/atom/{source['id']}").json()["links"]
    stated_row = next(
        row for row in explicit_links
        if row["target"] == stated_target["id"] and row["type"] == "supersedes"
    )
    assert stated_row["explicit"] is True
    assert isinstance(stated_row["actor"], str) and stated_row["actor"]

    bare_links = client.get(f"/api/mind/atom/{bare['id']}").json()["links"]
    bare_row = next(
        row for row in bare_links
        if row["target"] == bare_target["id"] and row["type"] == "supersedes"
    )
    assert bare_row["explicit"] is False
    assert bare_row["actor"] is None


def test_overview_phases_include_evict(client):
    phases = _overview(client)["phases"]
    assert any(row.get("phase") == "evict" for row in phases)


@pytest.mark.parametrize("inactive_state", ["ignored", "archived"])
def test_inactive_target_is_not_counted_as_hidden(client, inactive_state):
    source = _remember(client, "replacement for an inactive memory")
    target = _remember(client, "already inactive memory")
    linked = client.post(
        "/api/mind/link",
        json={"source_id": source["id"], "target_id": target["id"], "kind": "supersedes"},
    )
    assert linked.status_code == 200, linked.text

    conn = db.connect()
    try:
        conn.execute(
            "UPDATE m_rank SET state=? WHERE atom_id=?",
            (inactive_state, target["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    links = _overview(client)["links"]
    assert links["supersedes_explicit"] == 1
    assert links["hidden"] == 0


def test_dangling_edge_meta_rows_are_not_counted(client):
    before = _overview(client)["links"]

    conn = db.connect()
    try:
        conn.execute(
            """INSERT INTO m_edge_meta(edge_source,edge_target,edge_type,explicit,actor,created)
               VALUES('m_ghost_src','m_ghost_tgt','supersedes',1,'ses_ghost',1000),
                     ('m_ghost_src','m_ghost_tgt','related',0,'ses_ghost',1000)"""
        )
        conn.commit()
    finally:
        conn.close()

    after = _overview(client)["links"]
    assert after["explicit"] == before["explicit"]
    assert after["guessed"] == before["guessed"]
    assert after["unmarked"] == before["unmarked"]
    assert after["total"] == before["total"]


def test_link_feed_orders_explicit_before_bare_edge(client):
    bare_target = _remember(client, "bare link target", label="bare target")
    bare_source = _remember(
        client,
        "bare link source",
        label="bare source",
        links={"caused-by": [bare_target["id"]]},
    )
    stated_source = _remember(client, "stated link source", label="stated source")
    stated_target = _remember(client, "stated link target", label="stated target")
    linked = client.post(
        "/api/mind/link",
        json={
            "source_id": stated_source["id"],
            "target_id": stated_target["id"],
            "kind": "supersedes",
        },
    )
    assert linked.status_code == 200, linked.text

    feed = _overview(client)["link_feed"]
    stated_idx = next(
        i for i, row in enumerate(feed)
        if row.get("type") == "supersedes" and row.get("explicit") is True
    )
    bare_idx = next(
        i for i, row in enumerate(feed)
        if row.get("type") == "caused-by"
        and row.get("explicit") is False
        and row.get("source_label") == bare_source["label"]
    )
    assert stated_idx < bare_idx

