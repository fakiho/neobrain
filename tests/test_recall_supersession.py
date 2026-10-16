"""Recall hides atoms a caller explicitly superseded with a newer active atom (read-time, append-only)."""
from __future__ import annotations

from neobrain import mind

T = 1_700_000_000_000
Q = "openwrt firewall nftables"


def _pair(conn, add_atom):
    add_atom("old", created=T, label="openwrt firewall nftables",
             text="openwrt firewall nftables rules use iptables")
    add_atom("new", created=T + 1000, label="openwrt firewall nftables",
             text="openwrt firewall nftables replaced iptables")
    conn.execute("INSERT INTO m_edges(source,target,type) VALUES('new','old','supersedes')")
    conn.execute(
        "INSERT INTO m_edge_meta(edge_source,edge_target,edge_type,explicit,actor,created) "
        "VALUES('new','old','supersedes',1,NULL,?)",
        (T,),
    )
    conn.commit()


def _ids(conn, **kw):
    return {a["id"] for a in mind.recall(conn, Q, semantic=False, **kw)["atoms"]}


def test_superseded_hidden_by_default(conn, add_atom):
    _pair(conn, add_atom)
    assert _ids(conn) == {"new"}
    # append-only: the old atom is still stored
    assert conn.execute("SELECT 1 FROM m_atoms WHERE id='old'").fetchone()


def test_include_superseded_returns_both(conn, add_atom):
    _pair(conn, add_atom)
    assert _ids(conn, include_superseded=True) == {"old", "new"}


def test_inactive_superseder_does_not_hide(conn, add_atom):
    _pair(conn, add_atom)
    for state in ("ignored", "archived"):
        conn.execute("INSERT OR REPLACE INTO m_rank(atom_id,quality,served,interacted,state,updated) "
                     "VALUES('new',0.5,0,0,?,?)", (state, T))
        conn.commit()
        assert _ids(conn) == {"old"}


def test_no_supersedes_edges_unaffected(conn, add_atom):
    add_atom("a", created=T, label="openwrt firewall", text="openwrt firewall nftables one")
    add_atom("b", created=T, label="openwrt nftables", text="openwrt firewall nftables two")
    assert _ids(conn) == {"a", "b"}
    assert _ids(conn, include_superseded=True) == {"a", "b"}


def _edge(conn, source, target, kind="supersedes", *, explicit=None):
    """Insert a typed edge. ``explicit`` None means no meta row at all."""
    conn.execute(
        "INSERT INTO m_edges(source,target,type) VALUES(?,?,?)",
        (source, target, kind),
    )
    if explicit is not None:
        conn.execute(
            "INSERT INTO m_edge_meta(edge_source,edge_target,edge_type,explicit,actor,created) "
            "VALUES(?,?,?,?,?,?)",
            (source, target, kind, explicit, "test", T),
        )
    conn.commit()


def test_explicit_supersedes_hides_target(conn, add_atom):
    add_atom("old", created=T, label="openwrt firewall nftables",
             text="openwrt firewall nftables rules use iptables")
    add_atom("new", created=T + 1000, label="openwrt firewall nftables",
             text="openwrt firewall nftables replaced iptables")
    _edge(conn, "new", "old", explicit=1)

    assert _ids(conn) == {"new"}
    assert _ids(conn, include_superseded=True) == {"old", "new"}
    assert conn.execute("SELECT 1 FROM m_atoms WHERE id='old'").fetchone()


def test_explicit_zero_does_not_hide(conn, add_atom):
    add_atom("old", created=T, label="openwrt firewall nftables",
             text="openwrt firewall nftables rules use iptables")
    add_atom("new", created=T + 1000, label="openwrt firewall nftables",
             text="openwrt firewall nftables replaced iptables")
    _edge(conn, "new", "old", explicit=0)

    assert _ids(conn) == {"old", "new"}


def test_missing_meta_does_not_hide(conn, add_atom):
    add_atom("old", created=T, label="openwrt firewall nftables",
             text="openwrt firewall nftables rules use iptables")
    add_atom("new", created=T + 1000, label="openwrt firewall nftables",
             text="openwrt firewall nftables replaced iptables")
    _edge(conn, "new", "old")

    assert _ids(conn) == {"old", "new"}


def test_explicit_inactive_source_does_not_hide(conn, add_atom):
    add_atom("old", created=T, label="openwrt firewall nftables",
             text="openwrt firewall nftables rules use iptables")
    add_atom("new", created=T + 1000, label="openwrt firewall nftables",
             text="openwrt firewall nftables replaced iptables")
    _edge(conn, "new", "old", explicit=1)
    for state in ("ignored", "archived"):
        conn.execute(
            "INSERT OR REPLACE INTO m_rank(atom_id,quality,served,interacted,state,updated) "
            "VALUES('new',0.5,0,0,?,?)",
            (state, T),
        )
        conn.commit()
        assert _ids(conn) == {"old"}


def test_explicit_other_kind_does_not_hide(conn, add_atom):
    add_atom("old", created=T, label="openwrt firewall nftables",
             text="openwrt firewall nftables rules use iptables")
    add_atom("new", created=T + 1000, label="openwrt firewall nftables",
             text="openwrt firewall nftables replaced iptables")
    _edge(conn, "new", "old", "caused-by", explicit=1)

    assert _ids(conn) == {"old", "new"}


def test_explicit_self_edge_does_not_hide(conn, add_atom):
    add_atom("only", created=T, label="openwrt firewall nftables",
             text="openwrt firewall nftables rules use iptables")
    _edge(conn, "only", "only", explicit=1)

    assert _ids(conn) == {"only"}
    assert conn.execute("SELECT 1 FROM m_atoms WHERE id='only'").fetchone()
