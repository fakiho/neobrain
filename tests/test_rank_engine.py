"""Batch scorer, determinism, eviction state machine and promotion (SPEC §4.2/4.3)."""
from __future__ import annotations

import pytest

from neobrain import mind, rank

T = 1_700_000_000_000  # fixed "now" (ms) keeps recency deterministic
DAY = 86_400_000


def _rank_rows(conn):
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT atom_id,quality,served,interacted,last_served,state,state_since,updated "
            "FROM m_rank ORDER BY atom_id"
        )
    ]


def test_recompute_is_deterministic(conn, add_atom):
    add_atom("a1", created=T)
    add_atom("a2", created=T - 30 * DAY)
    assert rank.recompute(conn, now=T)["scored"] == 2
    first = _rank_rows(conn)
    rank.recompute(conn, now=T)
    assert _rank_rows(conn) == first
    assert all(row[5] == "active" for row in first)


def test_recompute_matches_hand_computed_quality(conn, add_atom):
    old = T - 84 * DAY  # 4 half-lives -> recency 0.0625
    add_atom("old", created=old)
    add_atom("hubby", created=T)
    for extra_hub in ("h2", "h3"):
        conn.execute("INSERT OR IGNORE INTO m_hubs(id,label,created) VALUES(?,?,?)", (extra_hub, extra_hub, T))
        conn.execute(
            "INSERT OR IGNORE INTO m_edges(source,target,type) VALUES('hubby',?, 'about')", (extra_hub,)
        )
    conn.commit()

    rank.recompute(conn, now=T)
    # old: feedback 0.5, connectivity ln2/ln4 = 0.5, recency 0.5**4 ->
    #   quality = 0.5 * (0.35 + 0.65*0.5**4) * (0.5 + 0.5*0.5)
    expected_old = 0.5 * (0.35 + 0.65 * (0.5 ** 4)) * 0.75
    q_old = conn.execute("SELECT quality FROM m_rank WHERE atom_id='old'").fetchone()[0]
    q_hubby = conn.execute("SELECT quality FROM m_rank WHERE atom_id='hubby'").fetchone()[0]
    assert q_old == pytest.approx(expected_old)
    # hubby: feedback 0.5, connectivity 1.0 (batch max), fresh -> 0.5
    assert q_hubby == pytest.approx(0.5)


def test_eviction_rule_and_exposure_boundary(conn, add_atom):
    add_atom("low_low", created=T)
    add_atom("low_high", created=T)
    add_atom("healthy", created=T)
    rank.recompute(conn, now=T)
    conn.execute("UPDATE m_rank SET quality=0.10, served=4 WHERE atom_id='low_low'")   # exposure 4
    conn.execute("UPDATE m_rank SET quality=0.10, served=5 WHERE atom_id='low_high'")  # exposure 5
    conn.execute("UPDATE m_rank SET quality=0.90, served=99 WHERE atom_id='healthy'")
    conn.commit()

    result = rank.run_eviction(conn, now=T)
    states = dict(conn.execute("SELECT atom_id,state FROM m_rank"))
    assert states["low_low"] == "ignored"
    assert states["low_high"] == "archived"
    assert states["healthy"] == "active"
    assert result == {"ignored": 1, "archived": 1, "now": T}


def test_eviction_quality_threshold_is_strict(conn, add_atom):
    add_atom("edge", created=T)
    rank.recompute(conn, now=T)
    conn.execute("UPDATE m_rank SET quality=0.15, served=99 WHERE atom_id='edge'")
    conn.commit()
    assert rank.run_eviction(conn, now=T) == {"ignored": 0, "archived": 0, "now": T}
    assert conn.execute("SELECT state FROM m_rank WHERE atom_id='edge'").fetchone()[0] == "active"


def test_eviction_never_promotes_or_deletes(conn, add_atom):
    add_atom("forgotten", created=T)
    rank.recompute(conn, now=T)
    conn.execute("UPDATE m_rank SET quality=0.01, state='ignored' WHERE atom_id='forgotten'")
    conn.commit()
    # A high-quality score must not resurrect an already-ignored atom: only
    # feedback promotes. Nor may eviction delete the row/atom.
    conn.execute("UPDATE m_rank SET quality=0.95 WHERE atom_id='forgotten'")
    conn.commit()
    rank.run_eviction(conn, now=T)
    assert conn.execute("SELECT state FROM m_rank WHERE atom_id='forgotten'").fetchone()[0] == "ignored"
    assert conn.execute("SELECT COUNT(*) FROM m_atoms WHERE id='forgotten'").fetchone()[0] == 1


def test_promotion_on_useful_or_used_signal(conn, add_atom):
    add_atom("x", created=T)
    rank.recompute(conn, now=T)
    conn.execute("UPDATE m_rank SET quality=0.01, state='ignored' WHERE atom_id='x'")
    conn.commit()

    # noise does not prove value: the atom stays forgotten.
    mind.feedback(conn, "x", "noise")
    assert conn.execute("SELECT state FROM m_rank WHERE atom_id='x'").fetchone()[0] == "ignored"

    mind.feedback(conn, "x", "useful")
    row = conn.execute("SELECT state,interacted FROM m_rank WHERE atom_id='x'").fetchone()
    assert row[0] == "active"  # promoted
    assert row[1] == 2         # both signals counted as interactions


def test_promotion_from_archived(conn, add_atom):
    add_atom("y", created=T)
    rank.recompute(conn, now=T)
    conn.execute("UPDATE m_rank SET quality=0.01, served=9, state='archived' WHERE atom_id='y'")
    conn.commit()
    mind.feedback(conn, "y", "used")
    assert conn.execute("SELECT state FROM m_rank WHERE atom_id='y'").fetchone()[0] == "active"


def test_record_served_increments_and_timestamps(conn, add_atom):
    add_atom("s", created=T)
    rank.record_served(conn, ["s"], now=T)
    rank.record_served(conn, ["s"], now=T + 5_000)
    served, last_served = conn.execute(
        "SELECT served,last_served FROM m_rank WHERE atom_id='s'"
    ).fetchone()
    assert served == 2
    assert last_served == T + 5_000


def test_recompute_atom_matches_batch(conn, add_atom):
    add_atom("x", created=T)
    add_atom("y", created=T - 10 * DAY)
    mind.feedback(conn, "x", "useful")
    rank.recompute(conn, now=T)
    q_batch = conn.execute("SELECT quality FROM m_rank WHERE atom_id='x'").fetchone()[0]
    q_atom = rank.recompute_atom(conn, "x", now=T)
    assert q_atom == pytest.approx(q_batch)


def test_recompute_atom_missing_returns_none(conn):
    assert rank.recompute_atom(conn, "does-not-exist", now=T) is None
