"""Append-only atom history: written in the same transaction as the change,
survives deletion of the atom. Temp SQLite stores only."""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from neobrain import cli, config, db, maintenance, mind
from neobrain.api import app


def _rows(conn, atom_id=None):
    sql = "SELECT atom_id,event,old_text,new_text,actor,reason FROM m_history"
    if atom_id:
        return [tuple(r) for r in conn.execute(sql + " WHERE atom_id=? ORDER BY id", (atom_id,))]
    return [tuple(r) for r in conn.execute(sql + " ORDER BY id")]


def test_remember_writes_one_add_row(conn):
    a = mind.remember(conn, "the build runs on port 9477", session_id="ses_abc", source="cli")
    assert _rows(conn) == [(a["id"], "add", None, "the build runs on port 9477", "ses_abc", None)]


def test_add_actor_falls_back_to_source(conn):
    a = mind.remember(conn, "a durable fact about dns", source="dream:deep")
    assert _rows(conn, a["id"])[0][4] == "dream:deep"


def test_deduped_restore_writes_no_history(conn):
    first = mind.remember(conn, "lesson: always verify the backup", source="dream:deep", dedupe=True)
    again = mind.remember(conn, "lesson: always verify the backup", source="dream:deep", dedupe=True)
    assert again.get("deduped") and again["id"] == first["id"]
    assert len(_rows(conn)) == 1


def test_forget_records_delete_and_row_survives(conn):
    a = mind.remember(conn, "temporary note to be removed", session_id="ses_x")
    mind.forget(conn, a["id"], "ses_x")
    assert conn.execute("SELECT COUNT(*) FROM m_atoms WHERE id=?", (a["id"],)).fetchone()[0] == 0
    assert _rows(conn, a["id"]) == [
        (a["id"], "add", None, "temporary note to be removed", "ses_x", None),
        (a["id"], "delete", "temporary note to be removed", None, "ses_x", None),
    ]


def test_forget_unknown_atom_writes_nothing(conn):
    mind.forget(conn, "m_doesnotexist")
    assert _rows(conn) == []


def test_history_is_oldest_first(conn):
    from neobrain.history import record_history
    record_history(conn, "m_a", "add", new_text="v1", ts=300)
    record_history(conn, "m_a", "delete", old_text="v1", ts=100)
    record_history(conn, "m_a", "add", new_text="v2", ts=100)
    record_history(conn, "m_other", "add", new_text="z", ts=1)
    conn.commit()
    h = mind.history(conn, "m_a")
    assert [(x["ts"], x["event"]) for x in h] == [(100, "delete"), (100, "add"), (300, "add")]
    assert set(h[0]) == {"id", "ts", "atom_id", "event", "old_text", "new_text", "actor", "reason"}
    assert mind.history(conn, "m_nothing") == []


# --- maintenance ----------------------------------------------------------


def _atom(conn, atom_id, source):
    conn.execute(
        "INSERT INTO m_atoms(id,label,type,created,text,source,tags,weight,hub,hash) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (atom_id, f"label {atom_id}", "observation", 1000, f"body {atom_id}", source,
         "agent", 0.5, "agent", "h-" + atom_id),
    )
    conn.execute("INSERT INTO m_rank(atom_id,updated) VALUES(?,?)", (atom_id, 1000))
    conn.commit()


@pytest.fixture
def seeded(conn):
    _atom(conn, "m_i1", "icm:a")
    _atom(conn, "m_i2", "icm:b")
    _atom(conn, "m_k1", "cli")
    return conn


def test_cleanup_records_one_delete_per_removed_atom(seeded):
    token = maintenance.plan_source_cleanup(seeded, "icm:")["confirm_token"]
    maintenance.apply_source_cleanup(seeded, "icm:", confirm=token, backup=False)
    rows = _rows(seeded)
    assert sorted(rows) == [
        ("m_i1", "delete", "body m_i1", None, "maintenance", "icm:"),
        ("m_i2", "delete", "body m_i2", None, "maintenance", "icm:"),
    ]
    assert mind.history(seeded, "m_k1") == []


def test_rolled_back_cleanup_leaves_no_history(seeded):
    token = maintenance.plan_source_cleanup(seeded, "icm:")["confirm_token"]
    seeded.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON m_ops WHEN NEW.op='maintenance' "
        "BEGIN SELECT RAISE(ABORT, 'boom'); END")
    seeded.commit()
    with pytest.raises(sqlite3.DatabaseError, match="boom"):
        maintenance.apply_source_cleanup(seeded, "icm:", confirm=token, backup=False)
    assert _rows(seeded) == []
    assert seeded.execute("SELECT COUNT(*) FROM m_atoms").fetchone()[0] == 3


# --- migration ------------------------------------------------------------


def test_v5_store_gains_history_table(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = db.connect(tmp_path / "old.db")
    db.init_db(c)
    c.execute("DROP TABLE m_history")
    c.execute("PRAGMA user_version = 5")
    c.commit()
    assert mind.history(c, "m_x") == []  # tolerates the missing table
    db.init_db(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 6
    assert c.execute("SELECT COUNT(*) FROM m_history").fetchone()[0] == 0
    c.close()


# --- CLI + API ------------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    monkeypatch.setattr(config.settings, "life_enabled", "0")
    return tmp_path


def test_cli_history(store, capsys):
    assert cli.main(["remember", "history via the cli smoke"]) == 0
    aid = json.loads(capsys.readouterr().out)["id"]
    assert cli.main(["forget", aid]) == 0
    capsys.readouterr()
    assert cli.main(["history", aid]) == 0
    events = json.loads(capsys.readouterr().out)
    assert [e["event"] for e in events] == ["add", "delete"]
    assert events[1]["old_text"] == "history via the cli smoke"


def test_api_history_get(store):
    with TestClient(app) as client:
        aid = client.post("/api/mind/remember", json={"text": "history via the api smoke"}).json()["id"]
        r = client.get("/api/mind/history", params={"atom_id": aid})
        assert r.status_code == 200
        body = r.json()
        assert body["atom_id"] == aid and body["count"] == 1
        assert body["events"][0]["event"] == "add"
        assert body["events"][0]["new_text"] == "history via the api smoke"
        assert client.get("/api/mind/history", params={"atom_id": "m_none"}).json()["count"] == 0
