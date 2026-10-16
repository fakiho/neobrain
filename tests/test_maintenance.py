"""Provenance-scoped cleanup: dry-run never mutates, apply is confirmed,
backed up and transactional. Everything runs on temp SQLite stores."""
from __future__ import annotations

import json
import sqlite3

import pytest

from neobrain import cli, config, db, maintenance


def _atom(conn, atom_id, *, source, hub="agent", tags=None, created=1000, rank=True, embed=True):
    conn.execute(
        "INSERT INTO m_atoms(id,label,type,created,text,source,tags,weight,hub,hash) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (atom_id, f"label {atom_id}", "observation", created, f"body {atom_id}", source,
         tags if tags is not None else hub, 0.5, hub, "h-" + atom_id),
    )
    conn.execute("INSERT OR IGNORE INTO m_hubs(id,label,created) VALUES(?,?,?)", (hub, hub, created))
    conn.execute("INSERT OR IGNORE INTO m_edges(source,target,type) VALUES(?,?,'about')", (atom_id, hub))
    if rank:
        conn.execute("INSERT INTO m_rank(atom_id,updated) VALUES(?,?)", (atom_id, created))
    if embed:
        conn.execute(
            "INSERT INTO m_embeddings(atom_id,model,dim,hash,vec,updated) VALUES(?,?,?,?,?,?)",
            (atom_id, "m", 1, "h", b"\x00", created),
        )
    conn.commit()


@pytest.fixture
def seeded(conn):
    # icm atoms: two in hub "icmhub" (only icm there), one in shared hub "agent"
    _atom(conn, "m_i1", source="icm:import:a", hub="icmhub")
    _atom(conn, "m_i2", source="icm:import:b", hub="icmhub", tags="icmhub,pin")
    _atom(conn, "m_i3", source="icm:other", hub="agent")
    # kept atoms
    _atom(conn, "m_k1", source="cli", hub="agent")
    _atom(conn, "m_k2", source=None, hub="agent")
    _atom(conn, "m_k3", source="ICM:upper", hub="agent")      # case differs: not matched
    _atom(conn, "m_k4", source="xicm:prefix", hub="agent")    # not a prefix match
    # a kept atom pointing at a doomed one, and feedback/ops for a doomed atom
    conn.execute("INSERT INTO m_edges(source,target,type) VALUES('m_k1','m_i3','supersedes')")
    conn.execute("INSERT INTO m_feedback(ts,atom_id,signal) VALUES(1,'m_i1','useful')")
    conn.execute("INSERT INTO m_ops(ts,op,atom_id) VALUES(1,'store','m_i1')")
    conn.commit()
    return conn


def _snapshot(conn):
    return {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1,2,3" if t == "m_edges"
                                           else f"SELECT * FROM {t} ORDER BY 1")]
        for t in ("m_atoms", "m_edges", "m_hubs", "m_rank", "m_embeddings", "m_feedback", "m_ops")
    }


def test_plan_reports_counts_and_does_not_mutate(seeded):
    before = _snapshot(seeded)
    plan = maintenance.plan_source_cleanup(seeded, "icm:")
    assert _snapshot(seeded) == before
    assert not seeded.in_transaction

    assert plan["mode"] == "plan" and plan["destructive"] is False
    assert plan["atoms"]["selected"] == 3
    assert plan["atoms"]["total"] == 7
    assert plan["atoms"]["remaining"] == 4
    assert plan["atoms"]["pinned_selected"] == 1
    assert plan["atoms"]["by_source"] == {"icm:import:a": 1, "icm:import:b": 1, "icm:other": 1}
    assert {s["id"] for s in plan["atoms"]["sample"]} == {"m_i1", "m_i2", "m_i3"}
    # 3 'about' edges + 1 retained->selected 'supersedes'
    assert plan["edges"]["touched"] == 4
    assert plan["edges"]["from_retained_to_selected"] == 1
    assert plan["edges"]["by_type"] == {"about": 3, "supersedes": 1}
    assert plan["dependents"] == {"embeddings": 3, "rank_rows": 3,
                                  "feedback_rows_kept": 1, "ops_rows_kept": 1}
    hubs = {h["id"]: h for h in plan["hubs"]["affected"]}
    assert hubs["icmhub"]["would_be_empty"] is True
    assert hubs["agent"]["would_be_empty"] is False
    assert plan["hubs"]["empty_after"] == ["icmhub"]
    assert plan["hubs"]["will_prune"] == []  # not requested
    assert plan["confirm_token"].startswith("delete-3-")


def test_plan_matches_like_semantics_for_the_default_prefix(seeded):
    like = {r[0] for r in seeded.execute("SELECT id FROM m_atoms WHERE source LIKE 'icm:%'")}
    # LIKE is case-insensitive, so it also catches ICM:upper; the plan must not.
    plan = maintenance.plan_source_cleanup(seeded, "icm:", sample=50)
    assert {s["id"] for s in plan["atoms"]["sample"]} == like - {"m_k3"}


def test_prefix_wildcards_are_literal(seeded):
    assert maintenance.plan_source_cleanup(seeded, "ic_:")["atoms"]["selected"] == 0
    assert maintenance.plan_source_cleanup(seeded, "%:")["atoms"]["selected"] == 0


@pytest.mark.parametrize("bad", ["", ":", "icm", "i", None])
def test_bad_prefix_rejected(seeded, bad):
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.plan_source_cleanup(seeded, bad)


def test_plan_is_repeatable_and_token_is_stable(seeded):
    a = maintenance.plan_source_cleanup(seeded, "icm:")
    b = maintenance.plan_source_cleanup(seeded, "icm:")
    assert a == b
    assert maintenance.plan_source_cleanup(seeded, "icm:", prune_empty_hubs=True)["confirm_token"] != a["confirm_token"]


def test_plan_with_nothing_selected_has_no_token(seeded):
    plan = maintenance.plan_source_cleanup(seeded, "nope:")
    assert plan["atoms"]["selected"] == 0 and plan["confirm_token"] is None


def test_apply_requires_correct_token_and_changes_nothing_without_it(seeded):
    before = _snapshot(seeded)
    for bad in (None, "", "yes", "delete-3-000000000000"):
        with pytest.raises(maintenance.MaintenanceError, match="confirmation"):
            maintenance.apply_source_cleanup(seeded, "icm:", confirm=bad, backup=False)
    assert _snapshot(seeded) == before


def test_apply_refuses_stale_token_after_store_changes(seeded):
    token = maintenance.plan_source_cleanup(seeded, "icm:")["confirm_token"]
    _atom(seeded, "m_i4", source="icm:late")
    with pytest.raises(maintenance.MaintenanceError, match="confirmation"):
        maintenance.apply_source_cleanup(seeded, "icm:", confirm=token, backup=False)
    assert seeded.execute("SELECT COUNT(*) FROM m_atoms WHERE source GLOB 'icm:*'").fetchone()[0] == 4


def test_apply_deletes_only_selected_with_backup(seeded, tmp_path):
    plan = maintenance.plan_source_cleanup(seeded, "icm:")
    res = maintenance.apply_source_cleanup(
        seeded, "icm:", confirm=plan["confirm_token"], backup_dir=tmp_path / "bk")
    assert res["deleted"] == {"edges": 4, "embeddings": 3, "rank_rows": 3, "atoms": 3, "hubs": 0}
    assert not seeded.in_transaction

    ids = {r[0] for r in seeded.execute("SELECT id FROM m_atoms")}
    assert ids == {"m_k1", "m_k2", "m_k3", "m_k4"}
    # kept atoms keep their rank/embedding rows and 'about' edges
    assert seeded.execute("SELECT COUNT(*) FROM m_rank").fetchone()[0] == 4
    assert seeded.execute("SELECT COUNT(*) FROM m_embeddings").fetchone()[0] == 4
    assert seeded.execute("SELECT COUNT(*) FROM m_edges WHERE type='supersedes'").fetchone()[0] == 0
    # history kept, hubs kept (no prune requested), maintenance op logged
    assert seeded.execute("SELECT COUNT(*) FROM m_feedback").fetchone()[0] == 1
    assert seeded.execute("SELECT COUNT(*) FROM m_hubs WHERE id='icmhub'").fetchone()[0] == 1
    assert seeded.execute("SELECT COUNT(*) FROM m_ops WHERE op='maintenance'").fetchone()[0] == 1

    # the backup is a complete copy taken *before* deletion
    bk = sqlite3.connect(res["backup"]["path"])
    try:
        assert bk.execute("SELECT COUNT(*) FROM m_atoms").fetchone()[0] == 7
        assert bk.execute("SELECT COUNT(*) FROM m_atoms WHERE source GLOB 'icm:*'").fetchone()[0] == 3
    finally:
        bk.close()
    assert res["backup"]["integrity"] == "ok"

    # repeatable: nothing left to do, second apply refuses cleanly
    with pytest.raises(maintenance.MaintenanceError, match="nothing to do"):
        maintenance.apply_source_cleanup(seeded, "icm:", confirm=plan["confirm_token"], backup=False)


def test_apply_prunes_only_hubs_left_empty(seeded):
    plan = maintenance.plan_source_cleanup(seeded, "icm:", prune_empty_hubs=True)
    assert plan["hubs"]["will_prune"] == ["icmhub"]
    res = maintenance.apply_source_cleanup(
        seeded, "icm:", confirm=plan["confirm_token"], prune_empty_hubs=True, backup=False)
    assert res["deleted"]["hubs"] == 1
    hubs = {r[0] for r in seeded.execute("SELECT id FROM m_hubs")}
    assert hubs == {"agent"}


def test_failure_rolls_back_everything(seeded, monkeypatch):
    token = maintenance.plan_source_cleanup(seeded, "icm:")["confirm_token"]

    # Fail late: after edges/embeddings/rank/atoms were deleted, at the m_ops insert.
    seeded.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON m_ops WHEN NEW.op='maintenance' "
        "BEGIN SELECT RAISE(ABORT, 'boom'); END")
    seeded.commit()
    before = _snapshot(seeded)
    with pytest.raises(sqlite3.DatabaseError, match="boom"):
        maintenance.apply_source_cleanup(seeded, "icm:", confirm=token, backup=False)
    assert not seeded.in_transaction
    assert _snapshot(seeded) == before

    # and it is retryable once the fault is gone
    seeded.execute("DROP TRIGGER boom")
    seeded.commit()
    res = maintenance.apply_source_cleanup(seeded, "icm:", confirm=token, backup=False)
    assert res["deleted"]["atoms"] == 3


def test_backup_failure_aborts_before_any_delete(seeded, tmp_path, monkeypatch):
    before = _snapshot(seeded)
    token = maintenance.plan_source_cleanup(seeded, "icm:")["confirm_token"]
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    with pytest.raises(OSError):
        maintenance.apply_source_cleanup(seeded, "icm:", confirm=token, backup_dir=blocker)
    assert _snapshot(seeded) == before


def test_in_memory_store_needs_explicit_no_backup():
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    db.init_db(mem)
    _atom(mem, "m_a", source="icm:x", embed=False)
    token = maintenance.plan_source_cleanup(mem, "icm:")["confirm_token"]
    with pytest.raises(maintenance.MaintenanceError, match="not file-backed"):
        maintenance.apply_source_cleanup(mem, "icm:", confirm=token)
    assert mem.execute("SELECT COUNT(*) FROM m_atoms").fetchone()[0] == 1
    maintenance.apply_source_cleanup(mem, "icm:", confirm=token, backup=False)
    assert mem.execute("SELECT COUNT(*) FROM m_atoms").fetchone()[0] == 0


# --- CLI ------------------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    c = db.connect(tmp_path / "neobrain.db")
    db.init_db(c)
    _atom(c, "m_i1", source="icm:a", hub="icmhub")
    _atom(c, "m_k1", source="cli", hub="agent")
    c.close()
    return tmp_path


def _count(path):
    c = sqlite3.connect(str(path))
    try:
        return c.execute("SELECT COUNT(*) FROM m_atoms").fetchone()[0]
    finally:
        c.close()


def test_cli_default_is_dry_run(store, capsys):
    assert cli.main(["maintain", "source-cleanup"]) == 0
    out = capsys.readouterr()
    plan = json.loads(out.out)
    assert plan["atoms"]["selected"] == 1 and plan["mode"] == "plan"
    assert plan["confirm_token"] in out.err  # the exact apply command is suggested
    assert _count(store / "neobrain.db") == 2
    assert not (store / "backups").exists()


def test_cli_apply_without_confirm_is_refused(store, capsys):
    assert cli.main(["maintain", "source-cleanup", "--apply"]) == 1
    assert "refused" in capsys.readouterr().err
    assert _count(store / "neobrain.db") == 2


def test_cli_confirm_without_apply_is_refused(store, capsys):
    assert cli.main(["maintain", "source-cleanup", "--confirm", "x"]) == 2
    assert _count(store / "neobrain.db") == 2


def test_cli_apply_with_token_backs_up_and_deletes(store, capsys):
    cli.main(["maintain", "source-cleanup"])
    token = json.loads(capsys.readouterr().out)["confirm_token"]
    assert cli.main(["maintain", "source-cleanup", "--apply", "--confirm", token]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["deleted"]["atoms"] == 1
    assert _count(store / "neobrain.db") == 1
    assert _count(receipt["backup"]["path"]) == 2
    assert (store / "backups").is_dir()


def test_cli_missing_store_is_not_created(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "neobrain.db")
    assert cli.main(["maintain", "source-cleanup"]) == 1
    assert not (tmp_path / "neobrain.db").exists()


# --- API ------------------------------------------------------------------


def test_api_plan_is_read_only_and_has_no_apply(store):
    from fastapi.testclient import TestClient

    from neobrain import api

    with TestClient(api.app) as client:
        r = client.get("/api/mind/maintenance/source-cleanup")
        assert r.status_code == 200
        assert r.json()["atoms"]["selected"] == 1
        assert client.get("/api/mind/maintenance/source-cleanup", params={"prefix": "bad"}).status_code == 400
        assert client.post("/api/mind/maintenance/source-cleanup").status_code == 405
    assert _count(store / "neobrain.db") == 2
