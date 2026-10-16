"""Explicit memory links: caller-stated edges are marked and never guessed over.

Hermetic: a temp store, no network. ``mind.link`` is the contract; the HTTP
route and CLI both call it with ``explicit=True``. These tests fail until that
extension lands — do not weaken them.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from neobrain import cli, config, mind
from neobrain.api import app


def _edges(conn, source: str, target: str, kind: str = "supersedes") -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM m_edges WHERE source=? AND target=? AND type=?",
        (source, target, kind),
    ).fetchone()[0]


def _meta(conn, source: str, target: str, kind: str = "supersedes"):
    return conn.execute(
        "SELECT explicit, actor FROM m_edge_meta "
        "WHERE edge_source=? AND edge_target=? AND edge_type=?",
        (source, target, kind),
    ).fetchone()


def _ops(conn, source: str) -> list:
    return list(conn.execute(
        "SELECT op, atom_id, label, query, session_id FROM m_ops WHERE atom_id=? ORDER BY id",
        (source,),
    ))


def _history(conn, source: str) -> list:
    return list(conn.execute(
        "SELECT event, new_text, actor, reason FROM m_history WHERE atom_id=? ORDER BY id",
        (source,),
    ))


def test_explicit_link_stores_edge_meta_one_op_and_one_history(conn, add_atom):
    add_atom("m_src", created=1)
    add_atom("m_tgt", created=2)

    result = mind.link(conn, "m_src", "m_tgt", "supersedes", "ses_1", explicit=True)

    assert result["source"] == "m_src"
    assert result["target"] == "m_tgt"
    assert result["type"] == "supersedes"
    assert result["explicit"] is True
    assert result["created"] is True
    assert _edges(conn, "m_src", "m_tgt") == 1
    meta = _meta(conn, "m_src", "m_tgt")
    assert meta is not None
    assert meta["explicit"] == 1
    assert meta["actor"] == "ses_1"
    assert _ops(conn, "m_src") == [("link", "m_src", "m_tgt", "supersedes", "ses_1")]
    assert _history(conn, "m_src") == [
        ("link", "supersedes -> m_tgt", "ses_1", "explicit"),
    ]


def test_repeating_an_explicit_link_writes_nothing(conn, add_atom):
    add_atom("m_src", created=1)
    add_atom("m_tgt", created=2)
    first = mind.link(conn, "m_src", "m_tgt", "supersedes", "ses_1", explicit=True)
    second = mind.link(conn, "m_src", "m_tgt", "supersedes", "ses_2", explicit=True)

    assert first["created"] is True
    assert second["created"] is False
    assert second["explicit"] is True
    assert _edges(conn, "m_src", "m_tgt") == 1
    assert _meta(conn, "m_src", "m_tgt")["explicit"] == 1
    # the original actor stays; a no-op re-link does not rewrite provenance
    assert _meta(conn, "m_src", "m_tgt")["actor"] == "ses_1"
    assert len(_ops(conn, "m_src")) == 1
    assert len(_history(conn, "m_src")) == 1


def test_guessed_link_is_inferred_and_not_explicit(conn, add_atom):
    add_atom("m_src", created=1)
    add_atom("m_tgt", created=2)

    result = mind.link(conn, "m_src", "m_tgt", "supersedes", "ses_guess", explicit=False)

    assert result["explicit"] is False
    assert result["created"] is True
    assert _edges(conn, "m_src", "m_tgt") == 1
    assert _meta(conn, "m_src", "m_tgt")["explicit"] == 0
    assert _history(conn, "m_src") == [
        ("link", "supersedes -> m_tgt", "ses_guess", "inferred"),
    ]
    assert len(_ops(conn, "m_src")) == 1


def test_explicit_link_upgrades_a_guessed_edge(conn, add_atom):
    add_atom("m_src", created=1)
    add_atom("m_tgt", created=2)
    mind.link(conn, "m_src", "m_tgt", "supersedes", "ses_guess", explicit=False)

    result = mind.link(conn, "m_src", "m_tgt", "supersedes", "ses_human", explicit=True)

    assert result["explicit"] is True
    assert result["created"] is False
    assert _edges(conn, "m_src", "m_tgt") == 1
    meta = _meta(conn, "m_src", "m_tgt")
    assert meta["explicit"] == 1
    assert meta["actor"] == "ses_human"
    # upgrade is a real change: one more op and one more history row, not a no-op
    assert len(_ops(conn, "m_src")) == 2
    assert [row["reason"] for row in _history(conn, "m_src")] == ["inferred", "explicit"]


def test_guessed_link_does_not_downgrade_an_explicit_one(conn, add_atom):
    add_atom("m_src", created=1)
    add_atom("m_tgt", created=2)
    mind.link(conn, "m_src", "m_tgt", "supersedes", "ses_human", explicit=True)

    result = mind.link(conn, "m_src", "m_tgt", "supersedes", "ses_guess", explicit=False)

    assert result["explicit"] is True
    assert result["created"] is False
    assert _edges(conn, "m_src", "m_tgt") == 1
    meta = _meta(conn, "m_src", "m_tgt")
    assert meta["explicit"] == 1
    assert meta["actor"] == "ses_human"
    # not an upgrade and not a new edge: a no-op, so provenance is untouched
    assert len(_ops(conn, "m_src")) == 1
    assert len(_history(conn, "m_src")) == 1
    assert _history(conn, "m_src")[0]["reason"] == "explicit"


@pytest.mark.parametrize(
    "source,target,kind",
    [
        ("m_missing", "m_tgt", "supersedes"),
        ("m_src", "m_missing", "supersedes"),
        ("m_src", "m_tgt", "not-a-kind"),
        ("m_src", "m_src", "supersedes"),
    ],
)
def test_bad_link_raises_and_writes_nothing(conn, add_atom, source, target, kind):
    add_atom("m_src", created=1)
    add_atom("m_tgt", created=2)
    edges_before = conn.execute("SELECT COUNT(*) FROM m_edges").fetchone()[0]

    with pytest.raises(ValueError):
        mind.link(conn, source, target, kind, "ses_bad", explicit=True)

    assert conn.execute("SELECT COUNT(*) FROM m_edges").fetchone()[0] == edges_before
    assert conn.execute("SELECT COUNT(*) FROM m_edge_meta").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM m_ops WHERE op='link'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM m_history WHERE event='link'"
    ).fetchone()[0] == 0


def test_forget_removes_the_edge_and_keeps_history(conn, add_atom):
    add_atom("m_src", created=1)
    add_atom("m_tgt", created=2)
    mind.link(conn, "m_src", "m_tgt", "supersedes", "ses_1", explicit=True)

    mind.forget(conn, "m_src", "ses_1")

    assert _edges(conn, "m_src", "m_tgt") == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM m_edges WHERE source=? OR target=?", ("m_src", "m_src")
    ).fetchone()[0] == 0
    history = _history(conn, "m_src")
    assert ("link", "supersedes -> m_tgt", "ses_1", "explicit") in history


# --- HTTP API ------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    monkeypatch.setattr(config.settings, "life_enabled", "0")
    with TestClient(app) as c:
        yield c


def test_api_link_rejects_bad_kind(client):
    src = client.post("/api/mind/remember", json={"text": "old gateway uses plaintext"}).json()
    tgt = client.post("/api/mind/remember", json={"text": "gateway now uses tls"}).json()

    bad = client.post("/api/mind/link", json={
        "source_id": src["id"], "target_id": tgt["id"], "kind": "not-a-kind", "session": "ses_api",
    })

    assert bad.status_code == 400
    assert "not-a-kind" in bad.json()["detail"]


def test_api_link_is_explicit(client):
    src = client.post("/api/mind/remember", json={"text": "old gateway uses plaintext"}).json()
    tgt = client.post("/api/mind/remember", json={"text": "gateway now uses tls"}).json()

    ok = client.post("/api/mind/link", json={
        "source_id": src["id"], "target_id": tgt["id"], "kind": "supersedes", "session": "ses_api",
    })

    assert ok.status_code == 200
    body = ok.json()
    assert body["source"] == src["id"]
    assert body["target"] == tgt["id"]
    assert body["type"] == "supersedes"
    assert body["explicit"] is True
    assert body["created"] is True


# --- CLI -----------------------------------------------------------------


def test_cli_link_records_explicit(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    assert cli.main(["remember", "old fact", "--source", "test"]) == 0
    src = json.loads(capsys.readouterr().out)["id"]
    assert cli.main(["remember", "new fact", "--source", "test"]) == 0
    tgt = json.loads(capsys.readouterr().out)["id"]

    assert cli.main(["link", src, tgt, "supersedes", "--session", "ses_cli"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["explicit"] is True
    assert out["source"] == src and out["target"] == tgt and out["type"] == "supersedes"

    from neobrain import db

    conn = db.connect(tmp_path / "neobrain.db")
    try:
        meta = conn.execute(
            "SELECT explicit FROM m_edge_meta WHERE edge_source=? AND edge_target=? AND edge_type=?",
            (src, tgt, "supersedes"),
        ).fetchone()
        assert meta is not None and meta["explicit"] == 1
    finally:
        conn.close()
