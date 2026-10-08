"""Session provenance: a memory links to the session it was saved from, and a
rating records both the rater's session and the memory's home session."""
from __future__ import annotations

from neobrain import mind


def test_remember_records_origin_session(conn):
    r = mind.remember(conn, "a durable fact", session_id="ses_A")
    got = conn.execute("SELECT session_id FROM m_atoms WHERE id=?", (r["id"],)).fetchone()[0]
    assert got == "ses_A"


def test_feedback_records_rater_and_origin(conn):
    r = mind.remember(conn, "memory saved here", session_id="ses_origin")
    res = mind.feedback(conn, r["id"], "useful", source="agent", session_id="ses_rater")
    assert res["session_id"] == "ses_rater"
    assert res["origin_session_id"] == "ses_origin"
    row = conn.execute(
        "SELECT session_id, origin_session_id FROM m_feedback WHERE atom_id=?", (r["id"],)
    ).fetchone()
    assert tuple(row) == ("ses_rater", "ses_origin")


def test_feedback_origin_null_for_unknown_atom(conn):
    res = mind.feedback(conn, "m_missing", "noise", session_id="ses_x")
    assert res["origin_session_id"] is None
    assert res["recorded"] is True


def test_memories_exposes_rank_and_unranked(conn):
    r = mind.remember(conn, "never used", session_id="ses_A")
    item = next(i for i in mind.memories(conn, limit=10)["items"] if i["id"] == r["id"])
    assert item["session_id"] == "ses_A"
    assert item["state"] == "active"
    assert item["quality"] == 0.5
    assert item["ranked"] is False
    assert (item["used"], item["useful"], item["noise"]) == (0, 0, 0)

    mind.feedback(conn, r["id"], "useful", session_id="ses_B")
    item = next(i for i in mind.memories(conn, limit=10)["items"] if i["id"] == r["id"])
    assert item["ranked"] is True
    assert item["useful"] == 1
