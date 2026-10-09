"""The inferred `unused` signal: a weak, bounded drag that never drives rank.

`unused` is what the plugin infers when the recall lane pushed a memory the
agent neither opened nor rated. It is deliberately *not* a verdict: it nudges
the recall multiplier a little and is logged for the rating-watch, but it must
not touch the per-atom rank recompute (it is high-volume) and any real
`useful`/`used` signal has to outweigh it.
"""
from __future__ import annotations

from neobrain import mind, rank

T = 1_700_000_000_000


def test_unused_is_a_bounded_weak_drag():
    assert mind._feedback_multiplier() == 1.0
    drag = mind._feedback_multiplier(unused=100)
    assert 1.0 - mind._FEEDBACK_CAP <= drag < 1.0  # bounded, never a real penalty
    # a single verdict (or being opened) outweighs any pile of inferred `unused`
    assert mind._feedback_multiplier(useful=1, unused=100) > 1.0
    assert mind._feedback_multiplier(used=5, unused=100) > 1.0


def test_unused_never_touches_the_rank_store(conn, add_atom):
    """It is soft and high-volume: the per-atom rank recompute must be skipped."""
    add_atom("a1", created=T, label="x", text="y")
    rank.recompute(conn, now=T)
    before = conn.execute("SELECT quality, interacted FROM m_rank WHERE atom_id='a1'").fetchone()
    assert mind.feedback(conn, "a1", "unused")["recorded"] is True
    after = conn.execute("SELECT quality, interacted FROM m_rank WHERE atom_id='a1'").fetchone()
    assert after == before


def test_rating_volume_counts_unused_but_not_as_rated(conn):
    atom = mind.remember(conn, "shown but never used", atype="observation", hubs=["neobrain"])
    mind.feedback(conn, atom["id"], "unused")
    snap = mind.rating_volume(conn, days=7)
    assert snap["unused"] >= 1
    assert snap["rated"] == 0  # inferred attention is not a subjective verdict
