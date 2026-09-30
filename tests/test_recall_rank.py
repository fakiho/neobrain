"""Recall integration: exclusion, exposure counters and pre-S4 ordering parity."""
from __future__ import annotations

from neobrain import mind, rank

T = 1_700_000_000_000


def _candidates(conn):
    return [
        {"id": r[0], "label": r[1], "type": r[2], "created": r[3], "text": r[4],
         "source": r[5], "tags": r[6] or "", "weight": r[7], "hub": r[8]}
        for r in conn.execute("SELECT id,label,type,created,text,source,tags,weight,hub FROM m_atoms")
    ]


def _expected_ids(conn, query, limit):
    """Pre-S4 ranking: score every atom with the unchanged scorer."""
    scored = mind._score_atoms(_candidates(conn), query, feedback=mind._feedback_counts(conn))
    return [a["id"] for _s, a in scored[:limit]]


def test_recall_parity_with_empty_rank_store(conn, add_atom):
    add_atom("a1", created=T, label="openwrt firewall nftables",
             text="the openwrt router uses nftables firewall rules on the lan")
    add_atom("a2", created=T, label="openwrt firewall", text="openwrt firewall nftables counters")
    add_atom("a3", created=T, label="bread", text="sourdough bread baking hydration")
    query = "openwrt firewall nftables"
    expected = _expected_ids(conn, query, 2)
    got = [a["id"] for a in mind.recall(conn, query, limit=2)["atoms"]]
    assert got == expected
    assert got and got[0] == "a1"


def test_recall_parity_with_all_active_rank_store(conn, add_atom):
    add_atom("a1", created=T, label="openwrt firewall nftables",
             text="the openwrt router uses nftables firewall rules on the lan")
    add_atom("a2", created=T, label="openwrt firewall", text="openwrt firewall nftables counters")
    add_atom("a3", created=T, label="bread", text="sourdough bread baking hydration")
    rank.recompute(conn, now=T)  # one active row per atom
    assert all(state == "active" for (state,) in conn.execute("SELECT state FROM m_rank"))

    query = "openwrt firewall nftables"
    expected = _expected_ids(conn, query, 2)
    got = [a["id"] for a in mind.recall(conn, query, limit=2)["atoms"]]
    assert got == expected


def test_forgotten_atom_is_excluded_from_recall(conn, add_atom):
    add_atom("best", created=T, label="openwrt firewall nftables",
             text="the openwrt router uses nftables firewall rules on the lan")
    add_atom("other", created=T, label="openwrt firewall", text="openwrt firewall nftables counters")
    rank.recompute(conn, now=T)

    first = [a["id"] for a in mind.recall(conn, "openwrt firewall nftables", limit=2)["atoms"]]
    assert first[0] == "best"

    conn.execute("UPDATE m_rank SET state='ignored' WHERE atom_id='best'")
    conn.commit()

    after = [a["id"] for a in mind.recall(conn, "openwrt firewall nftables", limit=2)["atoms"]]
    assert "best" not in after
    assert "other" in after


def test_recall_increments_served_and_last_served(conn, add_atom):
    add_atom("a1", created=T, label="openwrt firewall nftables", text="openwrt firewall nftables rules")
    rank.recompute(conn, now=T)
    assert conn.execute("SELECT served FROM m_rank WHERE atom_id='a1'").fetchone()[0] == 0

    mind.recall(conn, "openwrt firewall nftables", limit=1)
    served, last_served = conn.execute(
        "SELECT served,last_served FROM m_rank WHERE atom_id='a1'").fetchone()
    assert served == 1
    assert last_served is not None
