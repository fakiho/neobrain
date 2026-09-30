"""Default-config calibration for the forgetting engine (SPEC §4.2/§4.3).

This file guards the S4-review fix: the original additive blend let recency and
connectivity act as irreducible floors, so a fresh atom rated ``noise`` forever
still scored ~0.6 and the locked "low rank + high exposure -> archived" rule
could never fire from model feedback. Quality is now the product of three
bounded factors, and every factor can push a memory under the threshold.

The ``conn`` fixture pins the shipped defaults, so these assertions are exactly
the behaviour a default install gets.
"""
from __future__ import annotations

from neobrain import mind, rank

DAY = 86_400_000
T = 1_700_000_000_000


def test_noise_flood_archives_a_fresh_well_connected_atom(conn, add_atom):
    """Canonical locked case: served repeatedly, rated noise every time."""
    add_atom("noisy", created=T)
    for _ in range(30):
        mind.feedback(conn, "noisy", "noise")
    rank.recompute(conn, now=T)

    quality = conn.execute(
        "SELECT quality FROM m_rank WHERE atom_id='noisy'"
    ).fetchone()[0]
    assert quality < rank.params().ignore_below

    result = rank.run_eviction(conn, now=T)
    assert result["archived"] == 1
    assert conn.execute(
        "SELECT state FROM m_rank WHERE atom_id='noisy'"
    ).fetchone()[0] == "archived"
    # archived atoms are excluded from recall but their data is intact
    assert all(a["id"] != "noisy" for a in mind.recall(conn, "noisy")["atoms"])
    assert conn.execute(
        "SELECT COUNT(*) FROM m_atoms WHERE id='noisy'"
    ).fetchone()[0] == 1


def test_unrated_recent_atom_stays_active(conn, add_atom):
    add_atom("fresh", created=T)
    rank.recompute(conn, now=T)
    assert rank.run_eviction(conn, now=T) == {"ignored": 0, "archived": 0, "now": T}
    assert conn.execute(
        "SELECT state FROM m_rank WHERE atom_id='fresh'"
    ).fetchone()[0] == "active"


def test_useful_atom_beats_unrated(conn, add_atom):
    add_atom("good", created=T)
    mind.feedback(conn, "good", "useful")
    rank.recompute(conn, now=T)
    quality = conn.execute(
        "SELECT quality FROM m_rank WHERE atom_id='good'"
    ).fetchone()[0]
    # One `useful` against the smoothing prior lands just above the 0.5 neutral
    # (5 more would reach ~0.67): enough to outrank an unrated memory, never
    # enough to saturate. The prior strength is the rank_prior_n knob.
    assert 0.5 < quality < 0.6
    assert rank.run_eviction(conn, now=T)["archived"] == 0


def test_age_alone_ignores_a_low_exposure_atom(conn):
    """The other branch: a weak, long-idle, never-served atom is ignored."""
    conn.execute(
        "INSERT INTO m_atoms(id,label,type,created,text,source,tags,weight,hub,hash) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("old", "old", "observation", T - 300 * DAY, "body", "test", "", 0.5, "", "h"),
    )
    conn.commit()
    rank.recompute(conn, now=T)
    quality = conn.execute(
        "SELECT quality FROM m_rank WHERE atom_id='old'"
    ).fetchone()[0]
    assert quality < rank.params().ignore_below
    assert rank.run_eviction(conn, now=T)["ignored"] == 1
